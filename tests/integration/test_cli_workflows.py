"""Synthetic CLI workflows; never access hardware or network.

SPDX-License-Identifier: BSD-3-Clause
"""

import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from icat.catalogue.codec import decode
from icat.cli import main
from icat.reporting.journal import RunJournal
from icat.metadata.types import Metadata
from icat.roms.types import Rom
from icat.operations.options import Options
from icat.operations.session import run


@pytest.fixture
def existing_catalogue(options: Options, nes: bytes, provider: Any) -> list[dict]:
    (options.src / "Game.nes").write_bytes(nes)
    run(options, provider)
    return decode((options.dst / "catalogue.json").read_bytes())


def read_files_in(root: Path) -> dict[str, bytes]:
    return {f"{path.relative_to(root)}": path.read_bytes() for path in root.rglob("*") if path.is_file()}


def reject_unexpected_call(*args: object, **kwargs: object) -> None:
    raise AssertionError("This operation must not be called")


@pytest.mark.parametrize("command", [["metadata"], ["trash", "clean"], ["trash", "reset"]])
def test_source_free_commands_require_existing_catalogue_without_creating_it(
    options: Options, command: list[str], caplog: pytest.LogCaptureFixture,
) -> None:
    before = read_files_in(options.src.parent)

    result = main([*command, "--dst", f"{options.dst}", "--cache", f"{options.cache}"])

    assert result == 1
    assert "An existing catalogue is required" in caplog.text
    assert read_files_in(options.src.parent) == before
    assert not options.dst.exists() and not options.cache.exists()


def test_metadata_command_updates_existing_game_without_import_or_empty_directory(
    options: Options, existing_catalogue: list[dict], monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_source = read_files_in(options.src)
    original_image = options.dst / "images" / existing_catalogue[0]["image"]
    image_data = original_image.read_bytes()

    class Enrichment:
        def lookup(self, rom: Rom) -> Metadata:
            return Metadata(fields={"description": "New synthetic description"})

    monkeypatch.setattr("icat.operations.session.discover_sources", reject_unexpected_call)
    monkeypatch.setattr("icat.operations.imports.stage_source", reject_unexpected_call)
    result = run(replace(options, src=None), Enrichment())

    assert result.imported == result.removed == 0
    assert result.total == result.updated_existing == 1
    assert decode((options.dst / "catalogue.json").read_bytes())[0]["description"] == "New synthetic description"
    assert read_files_in(options.src) == original_source
    assert original_image.read_bytes() == image_data
    journal = json.loads(result.journal_path.read_bytes())
    assert journal["config"]["source"] is None
    assert journal["config"]["mode"] == "METADATA"


@pytest.mark.parametrize("action", ["clean", "reset"])
def test_standalone_trash_never_imports_or_fetches_metadata(
    options: Options, existing_catalogue: list[dict], monkeypatch: pytest.MonkeyPatch, action: str,
) -> None:
    entry = existing_catalogue[0]
    prefs = options.dst.parent / "prefs.json"
    document = {"trash": [entry["hash"]], "favourites": [entry["hash"]], "lang": "ru"}
    prefs.write_text(json.dumps(document))
    source_before = read_files_in(options.src)
    monkeypatch.setattr("icat.cli.build_provider", reject_unexpected_call)
    monkeypatch.setattr("icat.operations.session.discover_sources", reject_unexpected_call)
    monkeypatch.setattr("icat.operations.imports.stage_source", reject_unexpected_call)
    monkeypatch.setattr("icat.metadata.provider.Provider.iter_lookup", reject_unexpected_call)

    result = main(["trash", action, "--dst", f"{options.dst}", "--cache", f"{options.cache}"])

    assert result == 0
    assert read_files_in(options.src) == source_before
    image = options.dst / "images" / entry["image"]

    if action == "clean":
        assert not image.exists()
        assert decode((options.dst / "catalogue.json").read_bytes()) == []
        assert json.loads(prefs.read_bytes()) == document

    else:
        assert image.exists()
        assert decode((options.dst / "catalogue.json").read_bytes()) == existing_catalogue
        assert json.loads(prefs.read_bytes()) == {**document, "trash": []}

    assert not (options.dst / ".icat.lock").exists()
    assert not list(options.dst.glob(".icat-stage-*"))
    assert len(list((options.logs / f"trash-{action}").glob("*/journal.json"))) == 1


def test_standalone_trash_preserves_checksum_guard(
    options: Options, existing_catalogue: list[dict], caplog: pytest.LogCaptureFixture,
) -> None:
    entry = existing_catalogue[0]
    (options.dst.parent / "prefs.json").write_text(json.dumps({"trash": [entry["hash"]]}))
    image = options.dst / "images" / entry["image"]
    image.write_bytes(b"changed")
    original = (options.dst / "catalogue.json").read_bytes()

    result = main(["trash", "clean", "--dst", f"{options.dst}", "--cache", f"{options.cache}"])

    assert result == 1
    assert "SHA-256 mismatch" in caplog.text
    assert image.read_bytes() == b"changed"
    assert (options.dst / "catalogue.json").read_bytes() == original


def test_summary_distinguishes_sources_games_rejections_and_other_files(
    options: Options, nes: bytes, provider: Any, caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="icat")
    (options.src / "first.nes").write_bytes(nes)
    (options.src / "second.nes").write_bytes(nes)
    (options.src / "broken.nes").write_bytes(b"bad")
    (options.src / "notes.txt").write_text("Synthetic note")

    result = run(options, provider)

    assert (result.imported, result.total, result.accepted_sources, result.duplicate_sources) == (1, 1, 2, 1)
    assert (result.rejected, result.skipped_sources, result.other_source_files, result.removed) == (1, 0, 1, 0)
    assert "COMPLETED WITH REJECTIONS · exit 3" in caplog.text
    assert "Optional metadata missing" in caplog.text
    journal = json.loads(result.journal_path.read_bytes())
    assert journal["summary"]["duplicate_sources"] == 1
    assert journal["summary"]["accepted_sources"] == 2
    assert journal["summary"]["other_source_files"] == 1


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--http-timeout", "0"), ("--http-timeout", "nan"), ("--http-timeout", "301"),
        ("--http-retries", "6"), ("--http-max-wait", "3601"),
    ],
)
def test_invalid_http_arguments_exit_two_before_operation(
    option: str, value: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("icat.cli.run", reject_unexpected_call)
    monkeypatch.setattr("icat.cli.build_provider", reject_unexpected_call)

    with pytest.raises(SystemExit) as caught:
        main(["sync", option, value])

    assert caught.value.code == 2
    assert option in capsys.readouterr().err


def test_removed_http_request_interval_is_rejected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("icat.cli.run", reject_unexpected_call)
    monkeypatch.setattr("icat.cli.build_provider", reject_unexpected_call)

    with pytest.raises(SystemExit) as caught:
        main(["sync", "--http-request-interval", "0"])

    assert caught.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_sync_help_explains_copy_move_and_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:

    with pytest.raises(SystemExit) as caught:
        main(["sync", "--help"])

    assert caught.value.code == 0
    output = capsys.readouterr().out
    assert "Examples:" in output and "without mirroring" in output
    assert "entire accepted archive" in output and "3 rejected sources" in output


def test_cli_metadata_dispatches_without_source(
    options: Options, existing_catalogue: list[dict], caplog: pytest.LogCaptureFixture,
) -> None:
    result = main([
        "metadata", "--dst", f"{options.dst}", "--cache", f"{options.cache}",
        "--sources", "hasheous", "--http-offline",
    ])

    assert result == 0
    assert len(decode((options.dst / "catalogue.json").read_bytes())) == 1
    assert len(list((options.logs / "metadata").glob("*/journal.json"))) == 1


@pytest.mark.parametrize("action", ["clean", "reset"])
def test_standalone_trash_does_not_announce_success_before_final_checkpoint(
    options: Options, existing_catalogue: list[dict], monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, action: str,
) -> None:
    caplog.set_level(logging.INFO, logger="icat")
    entry = existing_catalogue[0]
    (options.dst.parent / "prefs.json").write_text(json.dumps({"trash": [entry["hash"]]}))
    original = RunJournal.record

    def fail_final(journal: RunJournal, state: str, *, force: bool = False) -> None:

        if force and state == "complete":
            assert not (options.dst / ".icat.lock").exists()
            raise OSError("Synthetic final checkpoint failure")

        return original(journal, state, force=force)

    monkeypatch.setattr(RunJournal, "record", fail_final)
    caplog.clear()

    result = main(["trash", action, "--dst", f"{options.dst}", "--cache", f"{options.cache}"])

    assert result == 1
    assert "COMPLETED" not in caplog.text
    assert "Synthetic final checkpoint failure" in caplog.text
    assert "not rolled back" in caplog.text


@pytest.mark.parametrize("command", [["metadata"], ["trash", "clean"], ["trash", "reset"]])
def test_source_free_commands_refuse_symlinked_destination(
    options: Options, existing_catalogue: list[dict], command: list[str], caplog: pytest.LogCaptureFixture,
) -> None:
    link = options.dst.parent / "linked-games"
    link.symlink_to(options.dst, target_is_directory=True)
    before = read_files_in(options.src.parent)

    result = main([*command, "--dst", f"{link}", "--cache", f"{options.cache}"])

    assert result == 1
    assert "Symlinked paths" in caplog.text
    assert read_files_in(options.src.parent) == before
