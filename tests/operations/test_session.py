import hashlib
import json
import logging
import re
import zipfile
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from icat.errors import CatalogueError
from icat.catalogue.codec import decode
from icat.databases.cache import DatasetCache
from icat.metadata.types import Metadata, LookupContext
from icat.metadata.provider import Provider
from icat.io.http import HttpClient
from icat.roms.types import Rom
from icat.metadata.sources.base import MetadataSource
from icat.operations.options import Options
from icat.operations.session import run
from icat.operations import publication, metadata as metadata_operations
from icat.io import files


def test_platform_filter_imports_and_moves_only_matching_raw_sources(
    options: Options, provider: Any, nes: bytes,
) -> None:
    selected = options.src / "selected.nes"
    selected.write_bytes(nes)
    other = options.src / "other.gb"
    other.write_bytes(b"GB")

    result = run(replace(options, move_roms=True, platforms=("NES",)), provider)

    entries = decode((options.dst / "catalogue.json").read_bytes())
    assert result.imported == result.total == result.removed == 1
    assert [entry["platform"] for entry in entries] == ["NES"]
    assert not selected.exists()
    assert other.read_bytes() == b"GB"
    journal = json.loads(next(options.logs.rglob("journal.json")).read_text())
    assert journal["config"]["platforms"] == ["NES"]


def test_move_publishes_sharded_catalogue_before_deleting_sources(options: Options, provider: Any, nes: bytes) -> None:
    options = replace(options, move_roms=True)
    source = options.src / "a.nes"
    source.write_bytes(nes)
    (options.src / "readme.txt").write_text("keep")

    result = run(options, provider)

    entries = decode((options.dst / "catalogue.json").read_bytes())
    entry = entries[0]
    digest = hashlib.sha256(nes).hexdigest()
    assert result.imported == result.total == result.removed == 1
    assert entry["image"] == f"NES/{digest[:2]}/a.nes"
    assert (options.dst / "images" / entry["image"]).read_bytes() == nes
    assert not source.exists()
    assert (options.src / "readme.txt").exists()
    assert not (options.dst / ".icat.lock").exists()
    assert not list(options.dst.glob(".icat-stage-*"))
    journal = json.loads(next(options.logs.rglob("journal.json")).read_text())
    assert journal["state"] == "complete"
    assert journal["removed"] == [f"{source}"]


@pytest.mark.parametrize("container", ["nes", "zip", "7z"])
@pytest.mark.parametrize("move", [False, True])
def test_nes_extra_data_is_published_unchanged_even_without_metadata(
    options: Options, provider: Any, nes: bytes, sevenzip_archive: Callable[..., Path], container: str, move: bool,
) -> None:
    data = nes + b"B" * 8192
    path = options.src / f"extra.{container}"

    if container == "zip":

        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("Extra.nes", data)

    elif container == "7z":
        sevenzip_archive(path, [("Extra.nes", data)])

    else:
        path.write_bytes(data)

    original = path.read_bytes()

    result = run(replace(options, move_roms=move), provider)

    assert (result.imported, result.rejected, result.removed) == (1, 0, int(move))
    entry = decode((options.dst / "catalogue.json").read_bytes())[0]
    assert entry["hash"] == hashlib.sha256(data).hexdigest()
    assert (options.dst / "images" / entry["image"]).read_bytes() == data

    if move:
        assert not path.exists()

    else:
        assert path.read_bytes() == original

    journal = json.loads(next(options.logs.rglob("journal.json")).read_text())
    event = next(entry for entry in journal["diagnostics"]["events"] if entry["outcome"] == "nes_trailing_data")
    assert event["bytes"] == 8192 and event["nonzero"] is True


def test_move_has_durable_per_source_intents_after_verified_publication(
    options: Options, provider: Any, nes: bytes, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = options.src / "a.nes"
    source.write_bytes(nes)
    original_unlink = Path.unlink
    observed = []

    def check_and_unlink(path: Path, *args: Any, **kwargs: Any) -> None:

        if path == source:
            entry = decode((options.dst / "catalogue.json").read_bytes())[0]
            assert (options.dst / "images" / entry["image"]).read_bytes() == nes
            log = next(options.logs.rglob("removals.jsonl"))
            intent = json.loads(log.read_text().splitlines()[-1])
            assert intent["phase"] == "intent"
            assert intent["path"] == f"{source}"
            assert intent["sha256"] == hashlib.sha256(nes).hexdigest()
            observed.append(intent)

        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", check_and_unlink)

    result = run(replace(options, move_roms=True), provider)

    assert result.removed == len(observed) == 1
    assert not list(options.logs.rglob("removals.jsonl"))


def test_zip_and_raw_duplicates_are_collapsed_and_sources_removed(options: Options, provider: Any, nes: bytes) -> None:
    options = replace(options, move_roms=True)
    (options.src / "a.nes").write_bytes(nes)

    with zipfile.ZipFile(options.src / "b.zip", "w") as archive:
        archive.writestr("renamed.nes", nes)
        archive.writestr("readme.txt", "extra")

    result = run(options, provider)

    assert (result.total, result.removed) == (1, 2)
    assert not list(options.src.iterdir())


def test_copy_is_the_default_for_raw_roms_and_archives(options: Options, provider: Any, nes: bytes) -> None:
    raw = options.src / "a.nes"
    raw.write_bytes(nes)
    zipped = options.src / "a.zip"

    with zipfile.ZipFile(zipped, "w") as archive:
        archive.writestr("a.nes", nes)
        archive.writestr("readme.txt", "keep source archive intact")

    original = zipped.read_bytes()

    result = run(options, provider)

    assert result.imported == result.total == 1
    assert result.removed == 0
    assert raw.read_bytes() == nes
    assert zipped.read_bytes() == original


def test_archive_without_name_tokens_is_retained_while_other_sources_are_moved(
    options: Options, provider: Any, nes: bytes, caplog: pytest.LogCaptureFixture,
) -> None:
    raw = options.src / "a.nes"
    raw.write_bytes(nes)
    zipped = options.src / "collection.zip"

    with zipfile.ZipFile(zipped, "w") as archive:
        archive.writestr("!!!.nes", nes)
        archive.writestr("---.nes", nes[:-1] + b"D")

    original = zipped.read_bytes()

    result = run(replace(options, move_roms=True), provider)

    assert result.imported == result.total == result.removed == 1
    assert not raw.exists()
    assert zipped.read_bytes() == original
    assert "multiple ROMs" in caplog.text


def test_copy_then_move_is_idempotent_and_keeps_preferences(options: Options, provider: Any, nes: bytes) -> None:
    (options.src / "a.nes").write_bytes(nes)
    prefs = options.dst.parent / "prefs.json"
    prefs.write_text("{\"custom\":true}")
    run(options, provider)
    catalogue = (options.dst / "catalogue.json").read_bytes()

    result = run(replace(options, move_roms=True), provider)
    again = run(options, provider)

    assert result.imported == 0 and result.removed == 1
    assert again.total == 1 and again.removed == 0
    assert (options.dst / "catalogue.json").read_bytes() == catalogue
    assert prefs.read_text() == "{\"custom\":true}"


@pytest.mark.parametrize("container", ["nes", "zip", "7z"])
@pytest.mark.parametrize("move", [False, True])
def test_bad_rom_is_retained_and_roms_before_and_after_it_are_imported(
    options: Options, provider: Any, nes: bytes, sevenzip_archive: Callable[..., Path],
    caplog: pytest.LogCaptureFixture, container: str, move: bool,
) -> None:
    before = options.src / "a.nes"
    after = options.src / "z.nes"
    before.write_bytes(nes)
    after.write_bytes(nes[:-1] + b"D")
    bad = options.src / f"bad.{container}"

    if container == "zip":

        with zipfile.ZipFile(bad, "w") as archive:
            archive.writestr("broken.nes", b"not a ROM")

    elif container == "7z":
        sevenzip_archive(bad, [("broken.nes", b"not a ROM")])

    else:
        bad.write_bytes(b"not a ROM")

    original = bad.read_bytes()

    result = run(replace(options, move_roms=move), provider)

    assert (result.total, result.imported, result.rejected) == (2, 2, 1)
    assert result.removed == (2 if move else 0)
    assert before.exists() is not move
    assert after.exists() is not move
    assert bad.read_bytes() == original
    assert "rejected; kept" in caplog.text
    entries = decode((options.dst / "catalogue.json").read_bytes())
    assert {entry["hash"] for entry in entries} == {
        hashlib.sha256(nes).hexdigest(), hashlib.sha256(nes[:-1] + b"D").hexdigest()
    }

    for entry in entries:
        assert hashlib.sha256((options.dst / "images" / entry["image"]).read_bytes()).hexdigest() == entry["hash"]

    journal = json.loads(next(options.logs.rglob("journal.json")).read_bytes())
    assert journal["state"] == "complete_with_rejections"
    assert journal["rejected_sources"][0]["path"] == f"{bad}"
    assert "Invalid iNES" in journal["rejected_sources"][0]["reason"]
    assert f"{bad}" not in journal["removed"]
    assert not list(options.dst.glob(".icat-stage-*"))


def test_bad_zip_container_is_rejected_without_stopping_later_sources(
    options: Options, provider: Any, nes: bytes,
) -> None:
    path = options.src / "bad.zip"
    path.write_bytes(b"not a ZIP")
    (options.src / "z.nes").write_bytes(nes)

    result = run(replace(options, move_roms=True), provider)

    assert (result.total, result.removed, result.rejected) == (1, 1, 1)
    assert path.read_bytes() == b"not a ZIP"


@pytest.mark.parametrize(
    "failure", [OSError("disk full"), CatalogueError("Source changed"), RuntimeError("bug")]
)
def test_fatal_staging_errors_are_not_misreported_as_rejected_roms(
    options: Options, provider: Any, nes: bytes, monkeypatch: pytest.MonkeyPatch, failure: BaseException,
) -> None:
    source = options.src / "a.nes"
    source.write_bytes(nes)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise failure

    monkeypatch.setattr("icat.operations.imports.stage_source", fail)

    with pytest.raises(type(failure), match=f"{failure}"):
        run(replace(options, move_roms=True), provider)

    assert source.read_bytes() == nes
    assert not (options.dst / "catalogue.json").exists()
    journal = json.loads(next(options.logs.rglob("journal.json")).read_bytes())
    assert journal["rejected_sources"] == []


def test_destination_corruption_blocks_overwrite_and_removal(options: Options, provider: Any, nes: bytes) -> None:
    source = options.src / "a.nes"
    source.write_bytes(nes)
    run(options, provider)
    entry = decode((options.dst / "catalogue.json").read_bytes())[0]
    target = options.dst / "images" / entry["image"]
    target.write_bytes(b"different")

    with pytest.raises(CatalogueError, match="refusing overwrite"):
        run(replace(options, move_roms=True), provider)

    assert source.read_bytes() == nes
    assert target.read_bytes() == b"different"


@pytest.mark.parametrize("failure", [OSError("disk full"), KeyboardInterrupt()])
def test_publish_failure_or_cancel_preserves_inputs(
    options: Options, provider: Any, nes: bytes, monkeypatch: pytest.MonkeyPatch, failure: BaseException,
) -> None:
    options = replace(options, move_roms=True)
    (options.src / "a.nes").write_bytes(nes)
    original = publication.write_atomic

    def fail_catalogue(path: Path, data: bytes) -> None:

        if path.name == "catalogue.json":
            raise failure

        original(path, data)

    monkeypatch.setattr(publication, "write_atomic", fail_catalogue)

    with pytest.raises(type(failure), match="disk full" if isinstance(failure, OSError) else "^$"):
        run(options, provider)

    assert (options.src / "a.nes").read_bytes() == nes
    assert not (options.dst / "catalogue.json").exists()
    assert not (options.dst / ".icat.lock").exists()
    assert not list(options.dst.glob(".icat-stage-*"))
    monkeypatch.setattr(publication, "write_atomic", original)
    assert run(options, provider).removed == 1


def test_source_changed_by_external_writer_is_retained(options: Options, nes: bytes) -> None:
    options = replace(options, move_roms=True)
    source = options.src / "a.nes"
    source.write_bytes(nes)

    class Mutating:
        def lookup(self, rom: Rom) -> Metadata:
            source.write_bytes(nes + b"changed")
            return Metadata()

    with pytest.raises(CatalogueError, match="Source changed"):
        run(options, Mutating())

    assert source.read_bytes() == nes + b"changed"
    assert (options.dst / "catalogue.json").exists()


def test_metadata_and_sharded_preview_are_published(options: Options, nes: bytes, png: bytes) -> None:
    (options.src / "a.nes").write_bytes(nes)

    class Described:
        def lookup(self, rom: Rom) -> Metadata:
            return Metadata({"description": "Synthetic text", "players": 2}, png, ["https://example.invalid/fixture"])

    run(options, Described())

    entry = decode((options.dst / "catalogue.json").read_bytes())[0]
    assert entry["description"] == "Synthetic text" and entry["players"] == 2
    digest = entry["hash"]
    assert (options.dst / "thumbs" / digest[:2] / f"{digest}.png").read_bytes() == png


def test_invalid_remote_fields_do_not_poison_catalogue(
    options: Options, nes: bytes, caplog: pytest.LogCaptureFixture,
) -> None:
    (options.src / "a.nes").write_bytes(nes)

    class BadMetadata:
        def lookup(self, rom: Rom) -> Metadata:
            return Metadata({"players": -1, "region": "Japan"})

    with caplog.at_level(logging.WARNING):
        run(options, BadMetadata())

    assert "invalid metadata ignored" in caplog.text
    entry = decode((options.dst / "catalogue.json").read_bytes())[0]
    assert entry["players"] is None


@pytest.mark.parametrize("path", ["overlap", "symlink", "locked"])
def test_unsafe_destination_fails(options: Options, provider: Any, nes: bytes, path: str) -> None:
    source = options.src / "a.nes"
    source.write_bytes(nes)

    if path == "overlap":
        options = replace(options, dst=options.src / "games")

    elif path == "symlink":
        options.dst.symlink_to(options.src, target_is_directory=True)

    else:
        options.dst.mkdir()
        (options.dst / ".icat.lock").write_text("existing")

    with pytest.raises(CatalogueError, match="overlap|Symlink|locked"):
        run(options, provider)

    assert source.read_bytes() == nes


def test_existing_catalogue_ignores_removed_version_and_unknown_keys(
    options: Options, provider: Any, nes: bytes,
) -> None:
    (options.src / "a.nes").write_bytes(nes)
    options.dst.mkdir()
    target = options.dst / "catalogue.json"
    target.write_bytes(json.dumps({"version": 999, "future": True, "games": []}).encode())

    run(options, provider)

    document = json.loads(target.read_bytes())
    assert set(document) == {"games"}
    assert len(document["games"]) == 1
    assert (options.src / "a.nes").exists()


def test_empty_source_can_enrich_previous_import_without_overwriting_manual_fields(
    options: Options, provider: Any, nes: bytes,
) -> None:
    (options.src / "a.nes").write_bytes(nes)
    run(replace(options, move_roms=True), provider)
    catalogue = options.dst / "catalogue.json"
    doc = json.loads(catalogue.read_text())
    doc["games"][0]["title"] = "My manual title"
    catalogue.write_text(json.dumps(doc))

    class Enrichment:
        def lookup(self, rom: Rom) -> Metadata:
            return Metadata({"title": "Remote title", "description": "New description"})

    result = run(options, Enrichment())

    entry = decode(catalogue.read_bytes())[0]
    assert entry["title"] == "My manual title" and entry["description"] == "New description"
    assert result.removed == result.imported == 0


def test_identical_bytes_with_conflicting_platforms_are_not_silently_merged(options: Options, provider: Any) -> None:
    (options.src / "a.gb").write_bytes(b"synthetic cartridge")
    (options.src / "b.gbc").write_bytes(b"synthetic cartridge")

    with pytest.raises(CatalogueError, match="Conflicting platforms"):
        run(options, provider)

    assert len(list(options.src.iterdir())) == 2


def test_repeated_sync_replaces_filename_only_description_but_preserves_manual_text(
    options: Options, nes: bytes, provider: Any,
) -> None:
    (options.src / "Game.nes").write_bytes(nes)
    run(options, provider)
    path = options.dst / "catalogue.json"
    document = json.loads(path.read_text())
    document["games"][0]["description"] = "Game (Japan)"
    path.write_text(json.dumps(document))

    class Described:
        def lookup(self, rom: Rom) -> Metadata:
            return Metadata({"description": "Actual synopsis."})

    run(options, Described())

    assert json.loads(path.read_text())["games"][0]["description"] == "Actual synopsis."
    document["games"][0]["description"] = "Manually written text."
    path.write_text(json.dumps(document))

    run(options, Described())

    assert json.loads(path.read_text())["games"][0]["description"] == "Manually written text."


def test_journal_has_sortable_utc_filename_configuration_changes_and_completeness(
    options: Options, nes: bytes, provider: Any,
) -> None:
    (options.src / "a.nes").write_bytes(nes)
    run(replace(options, move_roms=True), provider)

    class Updated:
        def lookup(self, rom: Rom) -> Metadata:
            return Metadata(
                {"title": "Resolved title", "year": 1991}, sources=["https://example.test/data?apikey=secret"],
                field_sources={"title": "fixture", "year": "fixture"},
                identifiers={"TheGamesDb:game": "42", "TheGamesDb:platform": "7"},
            )

    result = run(options, Updated())
    paths = sorted(options.logs.rglob("journal.json"))
    doc = json.loads(paths[-1].read_text())

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z(?:_\d{2})?", paths[-1].parent.name)
    assert paths[-1].parent.parent.name == "sync"
    assert doc["version"] == 4
    assert doc["finished_at"] >= doc["started_at"]
    assert doc["duration_seconds"] >= 0
    assert doc["config"]["source"] == f"{options.src}"
    assert doc["config"]["logs"] == f"{options.logs}"
    assert doc["initial_games"] == 1
    assert result.imported == result.removed == 0
    assert doc["metadata_fields"] == {hashlib.sha256(nes).hexdigest(): {"title": "fixture", "year": "fixture"}}
    assert doc["source_urls"] == {"s1": "https://example.test/data"}
    assert doc["metadata_identifiers"] == {
        hashlib.sha256(nes).hexdigest(): {"TheGamesDb:game": "42", "TheGamesDb:platform": "7"},
    }
    assert "secret" not in paths[-1].read_text()
    assert doc["summary"]["missing"]["year"] == 0
    assert doc["summary"]["missing"]["description"] == doc["summary"]["missing"]["thumbnail"] == 1
    assert doc["metadata_status"] == "complete"
    assert doc["metadata_completeness"] == "partial"
    assert doc["summary"]["existing_games_changed"] == 1
    assert doc["summary"]["new_games_changed"] == 0
    before = (options.dst / "catalogue.json").read_bytes()

    run(options, Updated())

    latest = json.loads(sorted(options.logs.rglob("journal.json"))[-1].read_text())
    assert latest["metadata_fields"] == {}
    assert latest["metadata_sources"] == latest["source_urls"] == {}
    assert latest["metadata_identifiers"] == {}
    assert latest["summary"]["games_changed"] == 0
    assert latest["summary"]["existing_games_changed"] == latest["summary"]["new_games_changed"] == 0
    assert (options.dst / "catalogue.json").read_bytes() == before


def test_thumbnail_only_change_keeps_mapping_provenance(
    options: Options, nes: bytes, png: bytes, provider: Any,
) -> None:
    (options.src / "a.nes").write_bytes(nes)
    run(options, provider)

    class Updated:
        def lookup(self, rom: Rom) -> Metadata:
            return Metadata(
                thumbnail=png, field_sources={"thumbnail": "fixture"},
                identifiers={"TheGamesDb:game": "42", "TheGamesDb:platform": "7"},
            )

    run(options, Updated())

    doc = json.loads(sorted(options.logs.rglob("journal.json"))[-1].read_text())
    digest = hashlib.sha256(nes).hexdigest()
    assert doc["metadata_fields"] == {digest: {"thumbnail": "fixture"}}
    assert doc["metadata_identifiers"] == {digest: {"TheGamesDb:game": "42", "TheGamesDb:platform": "7"}}
    assert doc["summary"]["existing_games_changed"] == 1
    assert doc["summary"]["new_games_changed"] == 0


@pytest.mark.parametrize("genre", ["Other", "Action", None])
def test_summary_separates_source_success_field_coverage_and_changed_new_records(
    options: Options, nes: bytes, png: bytes, genre: str | None, caplog: pytest.LogCaptureFixture,
) -> None:
    (options.src / "a.nes").write_bytes(nes)

    class Filled:
        def lookup(self, rom: Rom) -> Metadata:
            fields = {
                "title": "Resolved title", "region": [], "year": 1991, "players": 2,
                "genre": genre, "description": "A synthetic synopsis.",
            }
            return Metadata(fields, thumbnail=png, field_sources={name: "fixture" for name in fields})

    with caplog.at_level(logging.INFO, logger="icat"):
        run(options, Filled())

    doc = json.loads(next(options.logs.rglob("journal.json")).read_text())
    assert doc["metadata_status"] == "complete"
    expected = "complete" if genre == "Action" else "partial"
    assert doc["metadata_completeness"] == expected
    assert doc["summary"]["genre_other"] == int(genre == "Other")
    assert doc["summary"]["missing"]["genre"] == int(genre is None)
    assert doc["summary"]["existing_games_changed"] == 0
    assert doc["summary"]["new_games_changed"] == doc["summary"]["games_changed"] == 1
    assert f"Metadata sources: complete; catalogue metadata: {expected}" in caplog.text
    assert "changed existing/new records: 0/1" in caplog.text


def test_network_failure_details_survive_in_json_and_other_roms_continue(
    options: Options, nes: bytes, response_mock: Callable[..., Any],
) -> None:
    class Limited(MetadataSource):
        name = "limited"
        provides = frozenset({"year"})
        hosts = frozenset({"hasheous.org"})

        def fetch(self, context: LookupContext) -> Metadata:
            return Metadata(self.get_json("https://hasheous.org/test?apikey=secret") or {})

    (options.src / "a.nes").write_bytes(nes)
    (options.src / "b.nes").write_bytes(nes[:-1] + b"D")
    http = HttpClient(options.cache, max_retries=1, max_wait=0)
    pipeline = Provider([Limited(http, DatasetCache(options.cache / "datasets"))])

    with response_mock("GET https://hasheous.org/test?apikey=secret\nRetry-After: 120\n-> 429 :") as mock:
        result = run(replace(options, concurrency=1), pipeline)
        assert len(mock.calls) == 1

    path = next(options.logs.rglob("journal.json"))
    doc = json.loads(path.read_text())
    assert result.total == 2
    assert doc["state"] == "complete"
    assert doc["metadata_status"] == "partial"
    assert doc["metadata_completed"] == 2
    assert doc["diagnostics"]["hosts"]["hasheous.org"]["HTTP_429"] == 1
    assert doc["diagnostics"]["counts"]["limited"]["lookup.unavailable"] == 2
    assert any(event["reason"] == "wait_budget" for event in doc["diagnostics"]["network_failures"])
    assert doc["diagnostics"]["issues"] == []
    assert "secret" not in path.read_text()


@pytest.mark.parametrize("failure", [OSError("secret failure detail"), KeyboardInterrupt()])
def test_final_failure_and_cancel_are_recorded_with_the_failed_stage(
    options: Options, nes: bytes, failure: BaseException,
) -> None:
    (options.src / "a.nes").write_bytes(nes)

    class Broken:
        def lookup(self, rom: Rom) -> Metadata:
            raise failure

    with pytest.raises(
        type(failure), match="secret failure detail" if isinstance(failure, OSError) else "^$",
    ):
        run(options, Broken())

    path = next(options.logs.rglob("journal.json"))
    doc = json.loads(path.read_text())
    assert doc["state"] == ("interrupted" if isinstance(failure, KeyboardInterrupt) else "failed")
    assert doc["failed_stage"] == "metadata"
    assert doc["error"]["type"] == type(failure).__name__
    assert doc["finished_at"]
    assert "secret failure detail" not in path.read_text()
    assert (options.src / "a.nes").read_bytes() == nes


def test_source_skip_reasons_are_journaled_even_when_logging_is_disabled(
    options: Options, nes: bytes, provider: Any,
) -> None:

    with zipfile.ZipFile(options.src / "collection.zip", "w") as archive:
        archive.writestr("!!!.nes", nes)
        archive.writestr("---.nes", nes)

    (options.src / "unsupported.rar").write_bytes(b"not imported")
    (options.src / "padded.nes").write_bytes(nes + bytes(32))
    previous = logging.root.manager.disable
    try:
        logging.disable(logging.CRITICAL)
        result = run(options, provider)

    finally:
        logging.disable(previous)

    doc = json.loads(next(options.logs.rglob("journal.json")).read_text())
    outcomes = {event["outcome"] for event in doc["diagnostics"]["events"]}
    assert {"multiple_roms", "unsupported_format", "nes_zero_padding"}.issubset(outcomes)
    assert result.total == 1
    assert doc["summary"]["skipped"] == 1
    assert doc["summary"]["skipped_reasons"] == {"multiple_roms": 1}


@pytest.mark.parametrize("move", [False, True])
def test_unchanged_catalogue_reads_rom_at_two_boundaries_without_syncing_image_directories(
    options: Options, nes: bytes, provider: Any, monkeypatch: pytest.MonkeyPatch, move: bool,
) -> None:
    (options.src / "a.nes").write_bytes(nes)
    run(replace(options, move_roms=True), provider)
    catalogue = options.dst / "catalogue.json"
    original = catalogue.read_bytes()
    image = options.dst / "images" / decode(original)[0]["image"]
    inspections, hashes, directories = [], [], []
    inspect, hash_file, sync_dir = metadata_operations.inspect_rom, files.compute_sha256, files.sync_directory

    def inspect_once(path: Path, *args: Any, **kwargs: Any) -> Rom:
        inspections.append(path)
        return inspect(path, *args, **kwargs)

    def hash_once(path: Path) -> str:
        hashes.append(path)
        return hash_file(path)

    def sync_once(path: Path) -> None:
        directories.append(path)
        sync_dir(path)

    monkeypatch.setattr(metadata_operations, "inspect_rom", inspect_once)
    monkeypatch.setattr(files, "compute_sha256", hash_once)
    monkeypatch.setattr(files, "sync_directory", sync_once)

    result = run(replace(options, move_roms=move), provider)

    assert (result.imported, result.removed) == (0, 0)
    assert inspections == hashes == [image]
    assert not any(path == options.dst / "images" or options.dst / "images" in path.parents for path in directories)
    assert catalogue.read_bytes() == original


def test_new_images_share_directory_sync_before_catalogue_and_source_removal(
    options: Options, nes: bytes, provider: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:

    for name, data in (("a.nes", nes), ("b.nes", nes[:-1] + b"D")):
        (options.src / name).write_bytes(data)

    calls = []
    sync_dir, publish = files.sync_directory, publication.publish_catalogue

    def record_sync(path: Path) -> None:
        calls.append(path)
        sync_dir(path)

    def check_and_publish(path: Path, encoded: bytes, **kwargs: Any) -> None:

        for entry in decode(encoded):
            image = options.dst / "images" / entry["image"]
            assert image.read_bytes()
            assert all(parent in calls for parent in (image.parent, image.parent.parent, image.parent.parent.parent))

        assert len(list(options.src.glob("*.nes"))) == 2
        publish(path, encoded, **kwargs)

    monkeypatch.setattr(files, "sync_directory", record_sync)
    monkeypatch.setattr(publication, "publish_catalogue", check_and_publish)

    result = run(replace(options, move_roms=True), provider)

    counts = Counter(calls)
    assert result.removed == 2
    assert counts[options.dst / "images"] == counts[options.dst / "images" / "NES"] == 1


@pytest.mark.parametrize("mutation", ["corrupt", "replace", "symlink", "remove"])
def test_existing_image_changed_during_metadata_prevents_index_publication(
    options: Options, nes: bytes, provider: Any, mutation: str,
) -> None:
    (options.src / "a.nes").write_bytes(nes)
    run(replace(options, move_roms=True), provider)
    catalogue = options.dst / "catalogue.json"
    original = catalogue.read_bytes()
    target = options.dst / "images" / decode(original)[0]["image"]

    class Mutating:
        def lookup(self, rom: Rom) -> Metadata:

            if mutation != "corrupt":
                target.unlink()

            if mutation in {"corrupt", "replace"}:
                target.write_bytes(b"corrupt")

            elif mutation == "symlink":
                target.symlink_to(catalogue)

            return Metadata({"year": 1991})

    with pytest.raises(CatalogueError, match="refusing overwrite|Symlink|missing"):
        run(options, Mutating())

    assert catalogue.read_bytes() == original


@pytest.mark.parametrize("failure", [OSError("Synthetic image directory fsync failure"), KeyboardInterrupt()])
def test_image_directory_sync_failure_preserves_source_and_unpublished_index(
    options: Options, nes: bytes, provider: Any, monkeypatch: pytest.MonkeyPatch, failure: BaseException,
) -> None:
    source = options.src / "a.nes"
    source.write_bytes(nes)
    sync_dir = files.sync_directory

    def fail(path: Path) -> None:

        if path == options.dst / "images":
            raise failure

        sync_dir(path)

    monkeypatch.setattr(files, "sync_directory", fail)

    with pytest.raises(
        type(failure), match="Synthetic image directory fsync failure" if isinstance(failure, OSError) else "^$",
    ):
        run(replace(options, move_roms=True), provider)

    assert source.read_bytes() == nes
    assert not (options.dst / "catalogue.json").exists()


def test_move_readback_checks_only_images_of_sources_being_removed(
    options: Options, nes: bytes, provider: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = options.src / "old.nes"
    source.write_bytes(nes)
    run(replace(options, move_roms=True), provider)
    old = options.dst / "images" / decode((options.dst / "catalogue.json").read_bytes())[0]["image"]
    (options.src / "new.nes").write_bytes(nes[:-1] + b"D")
    hashes = []
    hash_file = files.compute_sha256

    def measure_hash(path: Path) -> str:
        hashes.append(path)
        return hash_file(path)

    monkeypatch.setattr(files, "compute_sha256", measure_hash)

    result = run(replace(options, move_roms=True), provider)

    assert result.removed == 1 and result.total == 2
    assert hashes.count(old) == 1
    new = next(path for path in (options.dst / "images").rglob("*.nes") if path != old)
    assert hashes.count(new) == 2


@pytest.mark.parametrize("boundary", ["image", "catalogue"])
def test_readback_corruption_at_publication_boundaries_prevents_source_removal(
    options: Options, nes: bytes, provider: Any, monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    source = options.src / "a.nes"
    source.write_bytes(nes)
    original_replace, original_write = publication.os.replace, publication.write_atomic

    def corrupt_image(src: Path | str, dst: Path | str) -> None:
        original_replace(src, dst)

        if boundary == "image" and Path(dst).suffix == ".nes":
            Path(dst).write_bytes(b"corruption")

    def corrupt_after_index(path: Path, data: bytes) -> None:
        original_write(path, data)

        if boundary == "catalogue" and path.name == "catalogue.json":
            entry = decode(data)[0]
            (options.dst / "images" / entry["image"]).write_bytes(b"corruption")

    monkeypatch.setattr(publication.os, "replace", corrupt_image)
    monkeypatch.setattr(publication, "write_atomic", corrupt_after_index)

    with pytest.raises(CatalogueError, match="Image verification failed|Destination changed"):
        run(replace(options, move_roms=True), provider)

    assert source.read_bytes() == nes
    journal = json.loads(next(options.logs.rglob("journal.json")).read_bytes())
    assert journal["state"] == "failed"
    assert journal["removed"] == []
