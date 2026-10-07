"""CLI messages with synthetic files, isolated logging and no network.

SPDX-License-Identifier: BSD-3-Clause
"""

import json
import logging
import re
import zipfile
from io import StringIO
from itertools import count
from pathlib import Path

import pytest

from icat.errors import CatalogueError, SourceError
from icat.catalogue.codec import decode
from icat.cli import main
from icat.reporting.diagnostics import Diagnostics
from icat.metadata.genres import resolve_genre
from icat.metadata.provider import Provider
from icat.operations.options import Options
from icat.operations.result import Result
from icat.reporting.terminal import MessageFormatter, PlainProgressHandler, RichProgressHandler


@pytest.mark.parametrize("mode", ["plain", "rich"])
@pytest.mark.parametrize("level", [logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL])
def test_cli_messages_have_severity_prefix_and_preserve_warning_colors(
    mode: str, level: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    stream = StringIO()
    handler = PlainProgressHandler(stream) if mode == "plain" else RichProgressHandler(stream, Diagnostics())
    record = logging.LogRecord("icat.test", level, "", 0, "%s — kept", (Path("/source/game \"x\".zip"),), None)
    original_args = record.args

    handler.handle(record)

    text = re.sub(r"\x1b\[[0-9;]*m", "", stream.getvalue()).strip()
    prefix = "ERROR " if level >= logging.ERROR else "WARN  " if level >= logging.WARNING else ""
    assert text == f"{prefix}\"game \\\"x\\\".zip\" — kept"
    assert record.args == original_args

    if mode == "rich" and level >= logging.WARNING:
        assert ("\x1b[31m" if level >= logging.ERROR else "\x1b[33m") in stream.getvalue()

    elif mode == "plain":
        assert "\x1b" not in stream.getvalue()


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/input/deep/game.nes", "\"game.nes\""),
        ("/output/images/NES/ab/game.nes", "\"images/NES/ab/game.nes\""),
        ("/output/thumbs/ab/game.png", "\"thumbs/ab/game.png\""),
        ("images/NES/ab/game.nes", "\"images/NES/ab/game.nes\""),
        ("/output-other/deep/game.nes", "\"game.nes\""),
    ],
)
def test_file_errors_use_destination_relative_paths_without_changing_durable_reason(path: str, expected: str) -> None:
    error = CatalogueError("Checksum mismatch", path=path)
    original = f"{error}"

    text = MessageFormatter(Path("/output")).format_error(error, status="Sync failed")

    assert text == f"{expected} — Sync failed: Checksum mismatch"
    assert f"{error}" == original == f"Checksum mismatch: {path}"


@pytest.mark.parametrize("name", ["collection.zip", "nested/member.nes"])
def test_rejection_does_not_repeat_source_but_quotes_a_distinct_archive_member(name: str) -> None:
    stream = StringIO()
    handler = PlainProgressHandler(stream)
    source = Path("/input/collection.zip")
    record = logging.LogRecord(
        "icat.test", logging.WARNING, "", 0, "%s — rejected; kept: %s",
        (source, SourceError("Invalid ROM", path=name)), None,
    )

    handler.handle(record)

    detail = "Invalid ROM" if name == source.name else "\"member.nes\" — Invalid ROM"
    assert stream.getvalue() == f"WARN  \"collection.zip\" — rejected; kept: {detail}\n"
    assert "/input" not in stream.getvalue()


def test_os_error_quotes_both_paths_and_does_not_expose_source_directory() -> None:
    error = OSError(5, "Input/output error", "/input/game.nes", None, "/output/images/NES/game.nes")

    text = MessageFormatter(Path("/output")).format_error(error, status="Sync failed")

    assert text == "\"game.nes\" — Sync failed: Input/output error -> \"images/NES/game.nes\""


def test_metadata_message_uses_only_rom_name_not_destination_path(caplog: pytest.LogCaptureFixture) -> None:
    result = resolve_genre("Unknown category", "NES/ab/Game.nes", Diagnostics())

    assert result is None
    assert caplog.records[-1].getMessage() == "\"Game.nes\" — unknown genre 'Unknown category'; using null"
    assert "NES/ab" not in caplog.text


@pytest.mark.parametrize("value", [None, "", "synthetic-secret"])
@pytest.mark.parametrize("selected", [True, False])
@pytest.mark.parametrize("offline", [True, False])
def test_cli_reports_only_selected_keyed_sources_without_exposing_values(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, value: str | None,
    selected: bool, offline: bool,
) -> None:

    if value is None:
        monkeypatch.delenv("ICAT_THEGAMESDB_API_KEY", raising=False)

    else:
        monkeypatch.setenv("ICAT_THEGAMESDB_API_KEY", value)

    def complete_run(options: Options, provider: Provider) -> Result:
        return Result(0, 0, 0, 0, 0)

    monkeypatch.setattr("icat.cli.run", complete_run)
    caplog.set_level(logging.INFO, logger="icat")
    args = ["sync", "--sources", "thegamesdb" if selected else "hasheous"]

    if offline:
        args.append("--http-offline")

    assert main(args) == 0

    messages = [record.getMessage() for record in caplog.records]
    expected = [
        "thegamesdb enabled: ICAT_THEGAMESDB_API_KEY is set" if value else
        "thegamesdb disabled: ICAT_THEGAMESDB_API_KEY not set; no enabled metadata source"
    ] if selected else []
    assert messages == expected
    assert "synthetic-secret" not in caplog.text


def test_cli_quotes_archive_members_rejections_and_destination_progress(
    options: Options, nes: bytes, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    archive = options.src / "Collection \"one\".zip"

    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("nested/Collection one.nes", nes)
        stream.writestr("nested/Collection one (Japan).nes", nes)

    rejected = options.src / "Bad.nes"
    rejected.write_bytes(b"invalid")

    def has_handlers() -> bool:
        return False

    monkeypatch.setattr(logging.getLogger("icat"), "hasHandlers", has_handlers)
    ticks = count(0, 10)

    def get_monotonic_time() -> float:
        return next(ticks)

    monkeypatch.setattr("icat.reporting.terminal.monotonic", get_monotonic_time)

    result = main([
        "sync", "--src", f"{options.src}", "--dst", f"{options.dst}", "--cache", f"{options.cache}",
        "--sources", "hasheous", "--http-offline", "--progress", "plain",
    ])

    output = capsys.readouterr()
    assert result == 3
    assert output.out == ""
    assert f"{options.src}" not in output.err
    assert (
        "\"Collection \\\"one\\\".zip\" — selected \"Collection one.nes\"; 2 ROMs, 2 matching title variants"
        in output.err
    )
    assert "\"Bad.nes\" — rejected; kept: Invalid iNES header" in output.err
    assert "nested/" not in output.err
    entry = decode((options.dst / "catalogue.json").read_bytes())[0]
    assert f"\n\"images/{entry["image"]}\" — cataloguing and verifying\n" in output.err
    assert "\n\"catalogue.json\" — syncing and cataloguing\n" in output.err
    assert "Catalogue & verify" in output.err
    assert "publish" not in output.err.lower()
    assert re.search(r"^WARN  \"Bad.nes\"", output.err, re.MULTILINE)
    journal = json.loads(next(options.logs.rglob("journal.json")).read_bytes())
    assert journal["rejected_sources"][0]["path"] == f"{rejected}"
    assert archive.is_file() and rejected.is_file()


def test_cli_destination_failure_starts_with_quoted_relative_filename(
    options: Options, nes: bytes, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    source = options.src / "Game.nes"
    source.write_bytes(nes)

    def has_handlers() -> bool:
        return False

    monkeypatch.setattr(logging.getLogger("icat"), "hasHandlers", has_handlers)
    args = [
        "sync", "--src", f"{options.src}", "--dst", f"{options.dst}", "--cache", f"{options.cache}",
        "--sources", "hasheous", "--http-offline", "--progress", "plain",
    ]
    assert main(args) == 0
    entry = decode((options.dst / "catalogue.json").read_bytes())[0]
    target = options.dst / "images" / entry["image"]
    target.write_bytes(b"changed")
    capsys.readouterr()

    assert main(args) == 1

    output = capsys.readouterr()
    assert f"\"images/{entry["image"]}\" — Sync failed: Existing image differs; refusing overwrite" in output.err
    assert f"{target}" not in output.err
    assert source.read_bytes() == nes
