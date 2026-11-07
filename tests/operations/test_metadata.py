"""Local stored-ROM defects do not authorize repair or stop independent games."""

import hashlib
import json
import logging
import os
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from icat.catalogue.codec import create_empty_entry, decode, encode
from icat.errors import CatalogueError
from icat.io.http import HttpClient
from icat.io.paths import get_thumbnail_name
from icat.metadata.provider import build_provider
from icat.metadata.types import Metadata
from icat.operations import metadata as metadata_operations
from icat.operations.options import Options
from icat.operations.session import run
from icat.roms.identity_cache import IdentityCache
from icat.roms.types import Rom


def forbid_rom_reads(monkeypatch: pytest.MonkeyPatch, image: Path) -> None:
    open_file = os.open

    def guarded_open(path: Any, *args: Any, **kwargs: Any) -> int:

        if path == image:
            pytest.fail("This operation must not open stored ROM contents")

        return open_file(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded_open)


@pytest.fixture
def indexed_games(options: Options, provider: Any, nes: bytes) -> list[dict]:
    (options.src / "bad.nes").write_bytes(nes)
    (options.src / "good.nes").write_bytes(nes[:-1] + b"G")
    run(replace(options, move_roms=True), provider)
    catalogue = options.dst / "catalogue.json"
    entries = decode(catalogue.read_bytes())
    entries[0].update(title="ZZZ", genre="Unmapped private genre", description="bad (Japan)")
    catalogue.write_bytes(encode(entries))
    return entries


@pytest.mark.parametrize("mode", ["metadata", "copy", "move", "move_archive"])
@pytest.mark.parametrize("fault", ["missing", "empty", "invalid", "digest"])
def test_bad_indexed_rom_is_preserved_while_other_games_continue(
    options: Options, indexed_games: list[dict], nes: bytes, png: bytes,
    caplog: pytest.LogCaptureFixture, mode: str, fault: str,
) -> None:
    caplog.set_level(logging.INFO, logger="icat")
    # A catalogue copied from another PC starts without cached lookup identities.
    (options.cache / IdentityCache.FILE_NAME).unlink()
    bad, good = indexed_games
    image = options.dst / "images" / bad["image"]
    damaged = {"empty": b"", "invalid": b"not a ROM", "digest": nes[:-1] + b"D"}

    if fault == "missing":
        image.unlink()

    else:
        image.write_bytes(damaged[fault])

    thumbnail = options.dst / "thumbs" / get_thumbnail_name(bad["hash"])
    thumbnail.parent.mkdir(parents=True, exist_ok=True)
    thumbnail.write_bytes(png)
    thumbnail_stamp = thumbnail.stat()
    preferences = options.dst.parent / "prefs.json"
    preferences.write_bytes(b'{"custom":true}\n')
    lookups = []

    class Enrichment:
        def lookup(self, rom: Rom) -> Metadata:
            lookups.append(rom.entry["hash"])
            return Metadata({"year": 1991, "description": "Verified synopsis."}, thumbnail=png)

    new = options.src / "new.nes"
    duplicate = options.src / "duplicate.nes"

    if mode != "metadata":
        new.write_bytes(nes[:-1] + b"N")

        if mode == "move_archive":
            duplicate = options.src / "duplicate.zip"

            with zipfile.ZipFile(duplicate, "w") as archive:
                archive.writestr("bad.nes", nes)
                archive.writestr("readme.txt", "keep this archive")

        else:
            duplicate.write_bytes(nes)

    original_source = duplicate.read_bytes() if duplicate.exists() else None
    selected = replace(options, src=None) if mode == "metadata" else replace(
        options, move_roms=mode in {"move", "move_archive"},
    )

    result = run(selected, Enrichment())

    entries = {entry["hash"]: entry for entry in decode((options.dst / "catalogue.json").read_bytes())}
    assert entries[bad["hash"]] == bad
    assert entries[good["hash"]]["year"] == 1991
    assert bad["hash"] not in lookups
    assert len(lookups) == (1 if mode == "metadata" else 2)
    assert result.total == len(entries) == (2 if mode == "metadata" else 3)
    assert result.skipped_images == 1
    assert result.rejected == 0
    assert result.missing_descriptions == 1
    assert result.metadata_status == "partial"
    assert result.removed == int(mode in {"move", "move_archive"})
    assert thumbnail.read_bytes() == png
    assert thumbnail.stat().st_ino == thumbnail_stamp.st_ino
    assert thumbnail.stat().st_mtime_ns == thumbnail_stamp.st_mtime_ns
    assert preferences.read_bytes() == b'{"custom":true}\n'

    if fault == "missing":
        assert not image.exists()

    else:
        assert image.read_bytes() == damaged[fault]

    if mode != "metadata":
        assert duplicate.read_bytes() == original_source
        assert new.exists() is (mode == "copy")

    journal = json.loads(result.journal_path.read_bytes())
    assert journal["state"] == "complete_with_skips"
    assert journal["summary"]["skipped_images"] == 1
    assert journal["summary"]["existing_games_changed"] == 1
    assert bad["hash"] not in journal["metadata_fields"]
    assert len(journal["skipped_images"]) == 1
    assert journal["skipped_images"][0]["path"] == f"{image}"
    assert journal["skipped_images"][0]["hash"] == bad["hash"]
    assert journal["skipped_images"][0]["reason"]

    if fault == "digest":
        assert journal["skipped_images"][0]["actual_hash"] == hashlib.sha256(damaged[fault]).hexdigest()

    if mode in {"move", "move_archive"}:
        assert journal["retained_sources"] == [f"{duplicate}"]

    snapshots = [record.icat_progress for record in caplog.records if hasattr(record, "icat_progress")]
    assert snapshots[-1].status == "complete_with_skips"
    assert snapshots[-1].inventory.skipped_images == 1
    assert snapshots[-1].percent == 100
    validation = next(step for step in snapshots[-1].steps if step.step.key == "validate")
    assert validation.completed == validation.total
    assert "COMPLETED WITH SKIPS · exit 3" in caplog.text
    assert not (options.dst / ".icat.lock").exists()
    assert not list(options.dst.glob(".icat-stage-*"))


def test_all_skipped_images_still_count_missing_metadata(options: Options, provider: Any) -> None:
    source = options.src / "bad.smc"
    source.write_bytes(b"synthetic SNES image")
    run(replace(options, move_roms=True), provider)
    catalogue = options.dst / "catalogue.json"
    original = catalogue.read_bytes()
    image = options.dst / "images" / decode(original)[0]["image"]
    image.write_bytes(b"")

    class NoLookups:
        def lookup(self, rom: Rom) -> Metadata:
            pytest.fail("Skipped image must not reach metadata providers")

    result = run(replace(options, src=None), NoLookups())

    assert result.total == result.skipped_images == result.missing_descriptions == result.missing_thumbnails == 1
    assert result.imported == result.updated_existing == result.removed == 0
    assert catalogue.read_bytes() == original
    assert image.read_bytes() == b""
    assert json.loads(result.journal_path.read_bytes())["state"] == "complete_with_skips"


@pytest.mark.parametrize("duplicate", [False, True])
def test_platform_mismatch_skips_existing_record_without_relabelling_it(
    options: Options, indexed_games: list[dict], provider: Any, nes: bytes, duplicate: bool,
) -> None:
    bad, good = indexed_games
    bad["platform"] = "SNES"
    catalogue = options.dst / "catalogue.json"
    catalogue.write_bytes(encode(indexed_games))

    if duplicate:
        (options.src / "duplicate.nes").write_bytes(nes)

    result = run(replace(options, move_roms=True), provider)

    entries = {entry["hash"]: entry for entry in decode(catalogue.read_bytes())}
    assert result.skipped_images == 1
    assert result.removed == 0
    assert result.rejected == int(duplicate)
    assert entries[bad["hash"]] == bad
    assert entries[good["hash"]]["hash"] == good["hash"]
    issue = json.loads(result.journal_path.read_bytes())["skipped_images"][0]
    assert "actual_hash" not in issue
    assert issue["actual_platform"] == "NES"

    if duplicate:
        assert (options.src / "duplicate.nes").read_bytes() == nes


@pytest.mark.parametrize("fault", ["empty", "missing"])
def test_skipped_rom_does_not_bypass_thumbnail_path_guards(
    options: Options, indexed_games: list[dict], provider: Any, tmp_path: Path, fault: str,
) -> None:
    bad = indexed_games[0]
    catalogue = options.dst / "catalogue.json"
    original = catalogue.read_bytes()
    image = options.dst / "images" / bad["image"]

    if fault == "missing":
        image.unlink()

    else:
        image.write_bytes(b"")

    outside = tmp_path / "outside.png"
    outside.write_bytes(b"preserve")
    thumbnail = options.dst / "thumbs" / get_thumbnail_name(bad["hash"])
    thumbnail.parent.mkdir(parents=True, exist_ok=True)
    thumbnail.symlink_to(outside)

    with pytest.raises(CatalogueError, match="Symlink destination"):
        run(replace(options, src=None), provider)

    assert catalogue.read_bytes() == original
    assert outside.read_bytes() == b"preserve"
    assert thumbnail.is_symlink()


@pytest.mark.parametrize("failure", [OSError("Synthetic I/O failure"), CatalogueError("Unsafe path"), RuntimeError("bug")])
def test_existing_image_io_guards_and_programming_errors_still_stop_the_run(
    options: Options, indexed_games: list[dict], provider: Any,
    monkeypatch: pytest.MonkeyPatch, failure: Exception,
) -> None:
    catalogue = options.dst / "catalogue.json"
    original = catalogue.read_bytes()
    (options.cache / IdentityCache.FILE_NAME).unlink()

    def fail(*args: Any, **kwargs: Any) -> Rom:
        raise failure

    monkeypatch.setattr(metadata_operations, "inspect_rom", fail)

    with pytest.raises(type(failure), match=f"{failure}"):
        run(replace(options, src=None), provider)

    assert catalogue.read_bytes() == original
    journal = json.loads(sorted((options.logs / "metadata").rglob("journal.json"))[-1].read_bytes())
    assert journal["state"] == "failed"
    assert journal["skipped_images"] == []


def test_image_changed_during_inspection_is_not_a_recoverable_content_error(
    options: Options, indexed_games: list[dict], provider: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspect = metadata_operations.inspect_rom
    catalogue = options.dst / "catalogue.json"
    original = catalogue.read_bytes()
    (options.cache / IdentityCache.FILE_NAME).unlink()

    def truncate_then_inspect(path: Path, *args: Any, **kwargs: Any) -> Rom:
        path.write_bytes(b"")
        return inspect(path, *args, **kwargs)

    monkeypatch.setattr(metadata_operations, "inspect_rom", truncate_then_inspect)

    with pytest.raises(CatalogueError, match="Indexed image changed while reading"):
        run(replace(options, src=None), provider)

    assert catalogue.read_bytes() == original


def test_disappeared_image_root_is_not_reported_as_individual_missing_games(
    options: Options, indexed_games: list[dict], provider: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = options.dst / "images"
    resolve = metadata_operations.resolve_relative_file

    def remove_root_after_resolution(parent: Path, name: str) -> Path:
        path = resolve(parent, name)

        if parent == root and root.exists():
            root.rename(options.dst / "disconnected-images")

        return path

    monkeypatch.setattr(metadata_operations, "resolve_relative_file", remove_root_after_resolution)

    with pytest.raises(OSError, match="No such file"):
        run(replace(options, src=None), provider)

    journal = json.loads(next((options.logs / "metadata").rglob("journal.json")).read_bytes())
    assert journal["state"] == "failed"
    assert journal["skipped_images"] == []


@pytest.mark.parametrize("mode", ["metadata", "sync"])
def test_cold_lookup_inspects_once_and_warm_lookup_does_not_open_roms(
    options: Options, provider: Any, nes: bytes, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    (options.src / "a.nes").write_bytes(nes)
    run(replace(options, move_roms=True), provider)
    entry = decode((options.dst / "catalogue.json").read_bytes())[0]
    image = options.dst / "images" / entry["image"]
    (options.cache / IdentityCache.FILE_NAME).unlink()
    inspections, lookups = [], []
    inspect = metadata_operations.inspect_rom

    def inspect_once(path: Path, *args: Any, **kwargs: Any) -> Rom:
        inspections.append(path)
        return inspect(path, *args, **kwargs)

    class Lookup:
        def lookup(self, rom: Rom) -> Metadata:
            lookups.append((rom.lookups, rom.crc32s, rom.full_lookup, rom.size))
            return Metadata()

    monkeypatch.setattr(metadata_operations, "inspect_rom", inspect_once)
    selected = replace(options, src=None) if mode == "metadata" else options

    cold = run(selected, Lookup())

    assert inspections == [image]
    assert cold.skipped_images == 0
    assert lookups[0][0]
    assert lookups[0][1]
    assert lookups[0][2]
    assert lookups[0][3] == len(nes)
    inspections.clear()
    forbid_rom_reads(monkeypatch, image)

    warm = run(selected, Lookup())

    assert inspections == []
    assert warm.skipped_images == 0
    assert len(lookups) == 2
    assert lookups[0] == lookups[1]


@pytest.mark.parametrize("stored_title, description, expected_description", [
    ("Manual title", "A genuine game synopsis.", "A genuine game synopsis."),
    ("ZZZ", "a (Japan)", None),
])
def test_no_external_work_keeps_local_normalization_without_reading_roms(
    options: Options, provider: Any, nes: bytes, png: bytes, monkeypatch: pytest.MonkeyPatch,
    stored_title: str, description: str, expected_description: str | None,
) -> None:
    (options.src / "a.nes").write_bytes(nes)
    run(replace(options, move_roms=True), provider)
    catalogue = options.dst / "catalogue.json"
    entry = decode(catalogue.read_bytes())[0]
    entry.update(
        title=stored_title, description=description, players=1, year=1991, region=["Japan"], genre="Action",
    )
    catalogue.write_bytes(encode([entry]))
    image = options.dst / "images" / entry["image"]
    original_stamp = image.stat()
    thumbnail = options.dst / "thumbs" / get_thumbnail_name(entry["hash"])
    thumbnail.parent.mkdir(parents=True)
    thumbnail.write_bytes(png)
    (options.cache / IdentityCache.FILE_NAME).unlink()
    configured = build_provider(HttpClient(options.cache), environ={}, source_names=["libretro"])
    forbid_rom_reads(monkeypatch, image)

    result = run(replace(options, src=None), configured)

    stored = decode(catalogue.read_bytes())[0]
    assert stored["title"] == ("a" if stored_title == "ZZZ" else stored_title)
    assert stored["description"] == expected_description
    assert result.skipped_images == 0
    assert image.stat().st_mtime_ns == original_stamp.st_mtime_ns
    assert thumbnail.read_bytes() == png
    assert not (options.cache / IdentityCache.FILE_NAME).exists()


def test_warm_metadata_uses_expected_identity_and_is_not_a_content_audit(
    options: Options, provider: Any, nes: bytes, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (options.src / "a.nes").write_bytes(nes)
    run(replace(options, move_roms=True), provider)
    catalogue = options.dst / "catalogue.json"
    entry = decode(catalogue.read_bytes())[0]
    image = options.dst / "images" / entry["image"]
    expected = IdentityCache(options.cache).get(image, entry)
    assert expected is not None
    changed = nes[:-1] + b"X"
    image.write_bytes(changed)

    class Enrichment:
        def lookup(self, rom: Rom) -> Metadata:
            assert rom.entry["hash"] == entry["hash"]
            assert rom.lookups == expected.lookups
            return Metadata({"year": 1991})

    forbid_rom_reads(monkeypatch, image)

    result = run(replace(options, src=None), Enrichment())

    assert result.skipped_images == 0
    assert decode(catalogue.read_bytes())[0]["year"] == 1991
    assert image.read_bytes() == changed


@pytest.mark.parametrize("platform", ["PSX", "SCD"])
def test_stored_disc_metadata_needs_no_content_hash_or_reinspection(
    options: Options, monkeypatch: pytest.MonkeyPatch, platform: str,
) -> None:
    data = b"Synthetic previously indexed disc container"
    entry = create_empty_entry("Legacy.chd", hashlib.sha256(data).hexdigest(), platform)
    image = options.dst / "images" / entry["image"]
    image.parent.mkdir(parents=True)
    image.write_bytes(data)
    catalogue = options.dst / "catalogue.json"
    catalogue.write_bytes(encode([entry]))

    class DiscLookup:
        def lookup(self, rom: Rom) -> Metadata:
            assert rom.lookups == []
            assert rom.entry["platform"] == platform
            return Metadata({"year": 1991})

    forbid_rom_reads(monkeypatch, image)

    result = run(replace(options, src=None), DiscLookup())

    assert result.skipped_images == 0
    assert decode(catalogue.read_bytes())[0]["year"] == 1991
    assert image.read_bytes() == data


@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("move", [False, True])
def test_duplicate_with_different_extension_uses_catalogue_format_lookups(
    options: Options, provider: Any, nes: bytes, warm: bool, move: bool,
) -> None:
    (options.src / "a.nes").write_bytes(nes)
    run(replace(options, move_roms=True), provider)
    catalogue = options.dst / "catalogue.json"
    entry = decode(catalogue.read_bytes())[0]
    image = options.dst / "images" / entry["image"]
    expected = IdentityCache(options.cache).get(image, entry)
    assert expected is not None
    assert len(expected.lookups) == 2
    source = options.src / "duplicate.unf"
    source.write_bytes(nes)

    if not warm:
        (options.cache / IdentityCache.FILE_NAME).unlink()

    class Enrichment:
        def lookup(self, rom: Rom) -> Metadata:
            assert rom.lookups == expected.lookups
            assert rom.crc32s == expected.crc32s
            assert rom.full_lookup == expected.full_lookup
            return Metadata({"year": 1991})

    result = run(replace(options, move_roms=move), Enrichment())

    assert result.total == 1
    assert result.skipped_images == result.rejected == 0
    assert result.removed == int(move)
    assert source.exists() is not move
    assert image.read_bytes() == nes
    stored = decode(catalogue.read_bytes())[0]
    assert stored["image"] == entry["image"]
    assert stored["year"] == 1991


def test_cached_size_mismatch_is_skipped_without_opening_rom(
    options: Options, indexed_games: list[dict], provider: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = indexed_games[0]
    image = options.dst / "images" / bad["image"]
    image.write_bytes(b"truncated ROM")
    forbid_rom_reads(monkeypatch, image)

    result = run(replace(options, src=None), provider)

    assert result.skipped_images == 1
    entries = {entry["hash"]: entry for entry in decode((options.dst / "catalogue.json").read_bytes())}
    assert entries[bad["hash"]] == bad
    assert image.read_bytes() == b"truncated ROM"
    issue = json.loads(result.journal_path.read_bytes())["skipped_images"][0]
    assert issue["reason"] == "Indexed image size mismatch"
    assert issue["actual_size"] == len(b"truncated ROM")
