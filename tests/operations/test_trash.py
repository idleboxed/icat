"""Trash operations on synthetic ROMs only; no hardware or network.

SPDX-License-Identifier: BSD-3-Clause
"""

import hashlib
import json
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from icat.errors import CatalogueError
from icat.catalogue.codec import decode
from icat.metadata.types import Metadata
from icat.io.paths import get_thumbnail_name
from icat.roms.types import Rom, Source
from icat.operations.options import Options
from icat.operations.session import run
from icat.catalogue.trash import TrashFile
from icat.operations import imports, publication, trash as trash_operations
from icat.catalogue import trash, preferences


@pytest.fixture
def library(options: Options, nes: bytes, png: bytes) -> list[dict]:
    (options.src / "a.nes").write_bytes(nes)
    (options.src / "b.nes").write_bytes(nes[:-1] + b"D")

    class WithThumbnails:
        def lookup(self, rom: Rom) -> Metadata:
            return Metadata(thumbnail=png)

    run(replace(options, move_roms=True), WithThumbnails())
    entries = decode((options.dst / "catalogue.json").read_bytes())
    prefs = {
        "favourites": [entry["hash"] for entry in entries],
        "trash": [entries[0]["hash"], "f" * 64, entries[0]["hash"]],
        "lang": "ru",
        "audio": {"volume": 42},
    }
    (options.dst.parent / "prefs.json").write_text(f"{json.dumps(prefs, indent=2)}\n")
    return entries


def test_trash_removes_only_indexed_marked_assets_and_keeps_preferences(
    options: Options, provider: Any, library: list[dict],
) -> None:
    prefs = options.dst.parent / "prefs.json"
    original = prefs.read_bytes()
    foreign = options.dst / "images/unindexed.nes"
    foreign.write_bytes(b"unrelated")
    first, second = library

    result = run(replace(options, trash=True), provider)
    repeated = run(replace(options, trash=True), provider)

    assert (result.trashed, result.total, result.imported, result.removed) == (1, 1, 0, 0)
    assert (repeated.trashed, repeated.total) == (0, 1)
    assert decode((options.dst / "catalogue.json").read_bytes()) == [second]
    assert not (options.dst / "images" / first["image"]).exists()
    assert not (options.dst / "thumbs" / get_thumbnail_name(first["hash"])).exists()
    assert (options.dst / "images" / second["image"]).is_file()
    assert (options.dst / "thumbs" / get_thumbnail_name(second["hash"])).is_file()
    assert prefs.read_bytes() == original
    assert foreign.read_bytes() == b"unrelated"
    reports = [json.loads(path.read_bytes()) for path in options.logs.rglob("journal.json")]
    report = next(entry for entry in reports if entry["trash"])
    assert report["state"] == "complete"
    assert len(report["trash_removed"]) == 2
    assert report["removed"] == []


def test_ordinary_sync_does_not_apply_or_parse_trash(options: Options, provider: Any, library: list[dict]) -> None:
    prefs = options.dst.parent / "prefs.json"
    prefs.write_bytes(b"not JSON: ordinary imports must not read preferences")

    result = run(options, provider)

    assert result.trashed == 0 and result.total == 2
    assert decode((options.dst / "catalogue.json").read_bytes()) == library
    assert prefs.read_bytes().startswith(b"not JSON")


@pytest.mark.parametrize("move", [False, True])
@pytest.mark.parametrize("zipped", [False, True])
def test_trash_completes_before_staging_and_source_returns_in_the_same_run(
    options: Options, provider: Any, library: list[dict], nes: bytes,
    monkeypatch: pytest.MonkeyPatch, move: bool, zipped: bool,
) -> None:
    first = library[0]
    image = options.dst / "images" / first["image"]
    preview = options.dst / "thumbs" / get_thumbnail_name(first["hash"])
    prefs = options.dst.parent / "prefs.json"
    original = prefs.read_bytes()
    source = options.src / ("a.zip" if zipped else "a.nes")

    if zipped:

        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("a.nes", nes)

    else:
        source.write_bytes(nes)

    stage = imports.stage_source
    calls = []

    def check_cleanup_before_copy(*args: Any, **kwargs: Any) -> Source | None:
        assert not image.exists() and not preview.exists()
        assert decode((options.dst / "catalogue.json").read_bytes()) == [library[1]]
        calls.append(True)
        return stage(*args, **kwargs)

    monkeypatch.setattr(imports, "stage_source", check_cleanup_before_copy)

    result = run(replace(options, trash=True, move_roms=move), provider)

    assert calls == [True]
    assert (result.trashed, result.imported, result.total, result.removed) == (1, 1, 2, int(move))
    assert image.read_bytes() == nes
    assert source.exists() is not move
    assert prefs.read_bytes() == original
    assert first["hash"] in json.loads(original)["trash"]


def test_reset_only_clears_trash_and_does_not_delete_assets(
    options: Options, provider: Any, library: list[dict],
) -> None:
    prefs = options.dst.parent / "prefs.json"
    expected = {**json.loads(prefs.read_bytes()), "trash": []}
    original_files = {path: path.read_bytes() for path in options.dst.rglob("*") if path.is_file()}

    result = run(replace(options, trash_reset=True), provider)

    assert result.trashed == 0 and result.total == 2
    assert json.loads(prefs.read_bytes()) == expected
    assert all(path.read_bytes() == data for path, data in original_files.items())
    assert not (options.dst.parent / ".prefs.json.tmp").exists()
    assert decode((options.dst / "catalogue.json").read_bytes()) == library


@pytest.mark.parametrize("content", [None, b"{\"lang\": \"ru\"}", b"{\"trash\": []}"])
@pytest.mark.parametrize("mode", ["trash", "trash_reset"])
def test_absent_or_empty_trash_is_a_noop_without_writing_preferences(
    options: Options, provider: Any, content: bytes | None, mode: str,
) -> None:
    prefs = options.dst.parent / "prefs.json"

    if content is not None:
        prefs.write_bytes(content)

    result = run(replace(options, **{mode: True}), provider)

    assert result.total == result.trashed == 0
    assert (prefs.read_bytes() if prefs.exists() else None) == content


@pytest.mark.parametrize(
    "content",
    [
        b"{",
        b"\xff",
        b"[]",
        b"null",
        b"{\"trash\":null}",
        b"{\"trash\":[42]}",
        b"{\"trash\":[\"bad\"]}",
        json.dumps({"trash": ["A" * 64]}).encode(),
        b" " * (1024 * 1024 + 1),
    ],
)
@pytest.mark.parametrize("mode", ["trash", "trash_reset"])
def test_bad_preferences_fail_before_import_or_deletion(
    options: Options, provider: Any, library: list[dict], nes: bytes, content: bytes, mode: str,
) -> None:
    prefs = options.dst.parent / "prefs.json"
    prefs.write_bytes(content)
    source = options.src / "new.nes"
    source.write_bytes(nes)
    original_files = {path: path.read_bytes() for path in options.dst.rglob("*") if path.is_file()}

    with pytest.raises(CatalogueError, match="preferences|Preferences|File exceeds"):
        run(replace(options, move_roms=True, **{mode: True}), provider)

    assert prefs.read_bytes() == content
    assert source.read_bytes() == nes
    assert all(path.read_bytes() == data for path, data in original_files.items())
    assert not (options.dst / ".icat.lock").exists()


@pytest.mark.parametrize("broken", ["image", "thumb", "image-parent", "prefs", "dangling-prefs"])
def test_symlinks_refuse_cleanup_before_any_deletion(
    options: Options, provider: Any, library: list[dict], tmp_path: Path, broken: str,
) -> None:
    first = options.dst / "images" / library[0]["image"]
    original = first.read_bytes()
    second = options.dst / "images" / library[1]["image"]
    prefs = options.dst.parent / "prefs.json"
    prefs.write_text(json.dumps({"trash": [entry["hash"] for entry in library]}))
    target = {
        "image": second,
        "thumb": options.dst / "thumbs" / get_thumbnail_name(library[1]["hash"]),
        "image-parent": second.parent,
        "prefs": prefs,
        "dangling-prefs": prefs,
    }[broken]
    outside = tmp_path / "outside"
    target.rename(outside)
    target.symlink_to(tmp_path / "missing" if broken == "dangling-prefs" else outside)

    with pytest.raises(CatalogueError, match="Symlink"):
        run(replace(options, trash=True), provider)

    assert first.read_bytes() == original
    assert outside.exists()
    assert target.is_symlink()


def test_corrupt_second_rom_prevents_deleting_the_first(options: Options, provider: Any, library: list[dict]) -> None:
    first = options.dst / "images" / library[0]["image"]
    second = options.dst / "images" / library[1]["image"]
    (options.dst.parent / "prefs.json").write_text(json.dumps({"trash": [entry["hash"] for entry in library]}))
    second.write_bytes(b"changed")

    with pytest.raises(CatalogueError, match="SHA-256 mismatch"):
        run(replace(options, trash=True), provider)

    assert first.is_file()
    assert second.read_bytes() == b"changed"
    assert decode((options.dst / "catalogue.json").read_bytes()) == library


@pytest.mark.parametrize("failure", [OSError("unlink denied"), KeyboardInterrupt()])
def test_partial_cleanup_keeps_index_paths_and_can_be_retried(
    options: Options, provider: Any, library: list[dict], monkeypatch: pytest.MonkeyPatch, failure: BaseException,
) -> None:
    first = options.dst / "images" / library[0]["image"]
    thumb = options.dst / "thumbs" / get_thumbnail_name(library[0]["hash"])
    unlink = Path.unlink

    def fail_thumb(path: Path, *args: Any, **kwargs: Any) -> None:

        if path == thumb:
            raise failure

        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(Path, "unlink", fail_thumb)

        with pytest.raises(type(failure), match="unlink denied" if isinstance(failure, OSError) else None):
            run(replace(options, trash=True), provider)

    assert not first.exists() and thumb.exists()
    assert decode((options.dst / "catalogue.json").read_bytes()) == library
    assert not (options.dst / ".icat.lock").exists()
    assert not list(options.dst.glob(".icat-stage-*"))
    reports = [json.loads(path.read_bytes()) for path in options.logs.rglob("journal.json")]
    report = next(entry for entry in reports if entry.get("failed_stage") == "removing_trash")
    assert report["state"] == ("failed" if isinstance(failure, OSError) else "interrupted")
    assert report["trash_removed"] == [f"{first}"]

    result = run(replace(options, trash=True), provider)

    assert result.trashed == 1 and result.total == 1
    assert not thumb.exists()


def test_catalogue_publish_failure_after_cleanup_is_recoverable(
    options: Options, provider: Any, library: list[dict], monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_atomic = publication.write_atomic

    def fail_catalogue(path: Path, data: bytes) -> None:

        if path.name == "catalogue.json":
            raise OSError("disk full")

        write_atomic(path, data)

    with monkeypatch.context() as context:
        context.setattr(publication, "write_atomic", fail_catalogue)

        with pytest.raises(OSError, match="disk full"):
            run(replace(options, trash=True), provider)

    assert decode((options.dst / "catalogue.json").read_bytes()) == library
    assert not (options.dst / "images" / library[0]["image"]).exists()

    result = run(replace(options, trash=True), provider)

    assert result.trashed == result.total == 1


def test_directory_sync_failure_stops_cleanup_before_unlink(
    options: Options, provider: Any, library: list[dict], monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_sync(path: Path) -> None:
        raise OSError("directory fsync unavailable")

    monkeypatch.setattr(trash, "sync_directory", fail_sync)

    with pytest.raises(OSError, match="fsync unavailable"):
        run(replace(options, trash=True), provider)

    assert all((options.dst / "images" / entry["image"]).exists() for entry in library)


@pytest.mark.parametrize("changed", ["prefs", "catalogue", "image"])
def test_external_changes_during_preflight_prevent_cleanup(
    options: Options, provider: Any, library: list[dict], monkeypatch: pytest.MonkeyPatch, changed: str,
) -> None:
    first = options.dst / "images" / library[0]["image"]
    path = {
        "prefs": options.dst.parent / "prefs.json",
        "catalogue": options.dst / "catalogue.json",
        "image": first,
    }[changed]
    original = path.read_bytes()
    prepare = trash_operations.prepare_trash

    def mutate_after_preflight(*args: Any) -> list[TrashFile]:
        files = prepare(*args)
        path.write_bytes(original + b" ")
        return files

    monkeypatch.setattr(trash_operations, "prepare_trash", mutate_after_preflight)

    with pytest.raises(CatalogueError, match="changed|modified"):
        run(replace(options, trash=True), provider)

    assert first.exists()
    assert path.read_bytes() == original + b" "


@pytest.mark.parametrize("mode", ["trash", "trash_reset"])
def test_pending_gui_preferences_write_is_not_overwritten(
    options: Options, provider: Any, library: list[dict], mode: str,
) -> None:
    pending = options.dst.parent / ".prefs.json.tmp"
    pending.write_bytes(b"another writer")

    with pytest.raises(CatalogueError, match="write is pending"):
        run(replace(options, **{mode: True}), provider)

    assert pending.read_bytes() == b"another writer"
    assert all((options.dst / "images" / entry["image"]).exists() for entry in library)


def test_conflicting_trash_modes_fail_without_creating_destination(options: Options, provider: Any) -> None:

    with pytest.raises(CatalogueError, match="mutually exclusive"):
        run(replace(options, trash=True, trash_reset=True), provider)

    assert not options.dst.exists()
    assert not options.cache.exists()


def test_rejected_import_does_not_undo_completed_trash_cleanup(
    options: Options, provider: Any, library: list[dict],
) -> None:
    (options.src / "broken.nes").write_bytes(b"invalid")
    prefs = options.dst.parent / "prefs.json"
    original = prefs.read_bytes()

    result = run(replace(options, trash=True), provider)

    assert result.rejected == 1
    assert decode((options.dst / "catalogue.json").read_bytes()) == [library[1]]
    assert not (options.dst / "images" / library[0]["image"]).exists()
    assert (options.src / "broken.nes").read_bytes() == b"invalid"
    assert prefs.read_bytes() == original


def test_reset_happens_before_staging_and_preserves_new_gui_changes(
    options: Options, provider: Any, library: list[dict], nes: bytes, monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefs = options.dst.parent / "prefs.json"
    (options.src / "a.nes").write_bytes(nes)
    stage = imports.stage_source

    def change_prefs_during_import(*args: Any, **kwargs: Any) -> Source | None:
        document = json.loads(prefs.read_bytes())
        assert document["trash"] == []
        document["trash"] = [library[1]["hash"]]
        prefs.write_text(json.dumps(document))
        return stage(*args, **kwargs)

    monkeypatch.setattr(imports, "stage_source", change_prefs_during_import)

    result = run(replace(options, trash_reset=True), provider)

    assert result.total == 2
    assert json.loads(prefs.read_bytes())["trash"] == [library[1]["hash"]]


@pytest.mark.parametrize("failure", [OSError("reset failed"), KeyboardInterrupt()])
def test_reset_write_failure_preserves_preferences_and_sources(
    options: Options, provider: Any, library: list[dict], nes: bytes,
    monkeypatch: pytest.MonkeyPatch, failure: BaseException,
) -> None:
    prefs = options.dst.parent / "prefs.json"
    original = prefs.read_bytes()
    source = options.src / "a.nes"
    source.write_bytes(nes)

    def fail_write(*args: Any) -> None:
        raise failure

    monkeypatch.setattr(preferences, "write_atomic", fail_write)

    with pytest.raises(type(failure), match="reset failed" if isinstance(failure, OSError) else None):
        run(replace(options, trash_reset=True, move_roms=True), provider)

    assert prefs.read_bytes() == original
    assert source.read_bytes() == nes
    assert decode((options.dst / "catalogue.json").read_bytes()) == library
    assert not (options.dst / ".icat.lock").exists()


def test_external_preferences_change_before_reset_is_preserved(
    options: Options, provider: Any, library: list[dict], monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefs = options.dst.parent / "prefs.json"
    changed = b"{\"trash\": [], \"lang\": \"en\"}"

    def change_during_preflight(path: Path) -> None:
        prefs.write_bytes(changed)

    monkeypatch.setattr(preferences, "sync_directory", change_during_preflight)

    with pytest.raises(CatalogueError, match="Preferences changed"):
        run(replace(options, trash_reset=True), provider)

    assert prefs.read_bytes() == changed
    assert decode((options.dst / "catalogue.json").read_bytes()) == library


def test_invalid_replacement_catalogue_prevents_any_deletion(
    options: Options, provider: Any, library: list[dict], monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_encoding(entries: list[dict]) -> bytes:
        raise CatalogueError("Catalogue exceeds IGUI's 16 MiB limit")

    monkeypatch.setattr(trash_operations, "encode", fail_encoding)

    with pytest.raises(CatalogueError, match="16 MiB limit"):
        run(replace(options, trash=True), provider)

    assert all((options.dst / "images" / entry["image"]).exists() for entry in library)
    assert decode((options.dst / "catalogue.json").read_bytes()) == library


def test_trash_does_not_block_initial_import_of_a_previously_marked_hash(
    options: Options, provider: Any, nes: bytes,
) -> None:
    prefs = options.dst.parent / "prefs.json"
    prefs.write_text(json.dumps({"trash": [hashlib.sha256(nes).hexdigest()]}))
    original = prefs.read_bytes()
    source = options.src / "a.nes"
    source.write_bytes(nes)

    result = run(replace(options, trash=True), provider)

    assert result.trashed == 0
    assert result.imported == result.total == 1
    assert source.read_bytes() == nes
    assert prefs.read_bytes() == original
