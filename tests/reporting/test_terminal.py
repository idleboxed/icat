"""In-memory terminal rendering only; no device or interactive shell required."""

import logging
import sys
from dataclasses import replace
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from icat.cli import configure_cli_logging
from icat.reporting.diagnostics import Diagnostics, NetworkActivity
from icat.reporting.progress import ProgressSnapshot, ProviderProgress, RunInventory, Step, StepProgress
from icat.reporting.terminal import PlainProgressHandler, RichProgressHandler, should_use_rich


@pytest.fixture
def snapshot() -> ProgressSnapshot:
    return ProgressSnapshot(
        (StepProgress(Step("scan", "Scan", "files"), "running", 4, 10),),
        0, "running", 0, 2, RunInventory(12, 10, 8),
    )


def create_progress_record(snapshot: ProgressSnapshot) -> logging.LogRecord:
    return logging.makeLogRecord({"levelno": logging.INFO, "levelname": "INFO", "icat_progress": snapshot})


@pytest.mark.parametrize("tty", [False, True])
@pytest.mark.parametrize("term", ["dumb", "unknown", "xterm-256color"])
@pytest.mark.parametrize("mode", ["auto", "rich", "plain"])
def test_display_selection(mode: str, tty: bool, term: str) -> None:
    class Stream(StringIO):
        def isatty(self) -> bool:
            return tty

    expected = mode == "rich" or (mode == "auto" and tty and term == "xterm-256color")

    assert should_use_rich(mode, Stream(), {"TERM": term}) is expected


def test_plain_output_bounds_repeated_updates_but_keeps_failures(
    snapshot: ProgressSnapshot, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def get_monotonic_time() -> float:
        return 10

    monkeypatch.setattr("icat.reporting.terminal.monotonic", get_monotonic_time)
    stream = StringIO()
    handler = PlainProgressHandler(stream)

    for _iteration in range(100):
        handler.handle(create_progress_record(snapshot))

    handler.handle(create_progress_record(replace(snapshot, status="failed")))
    handler.handle(logging.makeLogRecord({"levelno": logging.ERROR, "levelname": "ERROR", "msg": "Failure"}))

    lines = stream.getvalue().splitlines()
    assert len(lines) == 4
    assert "[1/1] Scan" in lines[0]
    assert "40%" in lines[0]
    assert "Plan" not in stream.getvalue()
    assert "12 files at scan" in lines[1]
    assert "8 confirmed records" in lines[1]
    assert lines[-1] == "ERROR Failure"
    assert "\x1b" not in stream.getvalue()


@pytest.mark.parametrize("width", [32, 80, 120])
def test_rich_displays_shared_counts_literal_names_and_ram_activity(
    snapshot: ProgressSnapshot, width: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def get_monotonic_time() -> float:
        return 10

    monkeypatch.setattr("icat.reporting.terminal.monotonic", get_monotonic_time)
    diagnostics = Diagnostics()
    handler = RichProgressHandler(StringIO(), diagnostics)
    snapshot = replace(snapshot, steps=(replace(snapshot.current, item="[red]name[/red]\x1b[2J"),))
    handler.handle(create_progress_record(snapshot))
    output = StringIO()
    console = Console(file=output, width=width, color_system=None)

    with diagnostics.track_activity(NetworkActivity("example.test", "rate_limit", 28)):
        console.print(handler.render())

    text = output.getvalue()
    assert "40%" in text
    assert "confirmed records" in text
    assert "HTTP: 0 active" in text
    assert "18s" in text
    assert "\x1b" not in text
    assert all(len(line) <= width for line in text.splitlines())

    if width >= 80:
        assert "[red]name[/red]\\x1b[2J" in text


def test_diagnostics_activity_is_nested_and_not_added_to_durable_history() -> None:
    diagnostics = Diagnostics()
    waiting = NetworkActivity("example.test", "pacing", 1)
    active = NetworkActivity("example.test", "request")
    before = diagnostics.get_snapshot()

    with diagnostics.track_activity(waiting):
        assert diagnostics.get_activity_snapshot() == (waiting,)

        with pytest.raises(KeyboardInterrupt, match="^$"):

            with diagnostics.track_activity(active):
                assert diagnostics.get_activity_snapshot() == (active,)
                raise KeyboardInterrupt

        assert diagnostics.get_activity_snapshot() == (waiting,)

    assert diagnostics.get_activity_snapshot() == ()
    assert diagnostics.get_snapshot() == before


@pytest.mark.parametrize("mode", ["plain", "rich"])
def test_cli_handler_is_scoped_and_does_not_capture_stdout(
    mode: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    package = logging.getLogger("icat")
    handlers, level = list(package.handlers), package.level
    root_handlers = list(logging.getLogger().handlers)
    def has_handlers() -> bool:
        return False

    monkeypatch.setattr(package, "hasHandlers", has_handlers)
    original_stdout = sys.stdout

    for _iteration in range(2):

        with configure_cli_logging(mode):
            assert sys.stdout is original_stdout
            package.warning("Literal [red]warning[/red]")

    assert package.handlers == handlers
    assert package.level == level
    assert logging.getLogger().handlers == root_handlers
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("Literal [red]warning[/red]") == 2


def test_existing_logging_configuration_is_left_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    package = logging.getLogger("icat")
    handlers, level = list(package.handlers), package.level
    def has_handlers() -> bool:
        return True

    monkeypatch.setattr(package, "hasHandlers", has_handlers)

    with configure_cli_logging("rich"):
        assert package.handlers == handlers
        assert package.level == level


@pytest.mark.parametrize("mode", ["plain", "rich"])
def test_repeated_warnings_are_grouped_but_errors_are_never_suppressed(mode: str) -> None:
    output = StringIO()
    handler = PlainProgressHandler(output) if mode == "plain" else RichProgressHandler(output, Diagnostics())
    warning = logging.makeLogRecord({"levelno": logging.WARNING, "msg": "Provider unavailable"})
    error = logging.makeLogRecord({"levelno": logging.ERROR, "msg": "Unable to save"})

    for _iteration in range(50):
        handler.handle(warning)

    handler.handle(error)
    handler.handle(error)
    handler.flush_repeats()
    handler.flush_repeats()

    text = output.getvalue()
    assert text.count("Provider unavailable") == 2
    assert "repeated 49 more times" in text
    assert text.count("Unable to save") == 2
    assert "WARN" in text and "ERROR" in text


def test_provider_progress_is_visible_before_overall_metadata_results(
    snapshot: ProgressSnapshot, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = iter((0, 1, 6, 7))
    def get_monotonic_time() -> float:
        return next(ticks)

    monkeypatch.setattr("icat.reporting.terminal.monotonic", get_monotonic_time)
    output = StringIO()
    handler = PlainProgressHandler(output)
    handler.handle(create_progress_record(snapshot))

    for event in (
        ProviderProgress("first", 1, 2, 0, 20),
        ProviderProgress("first", 1, 2, 10, 20),
        ProviderProgress("second", 2, 2, 0, 20),
    ):
        handler.handle(logging.makeLogRecord({"levelno": logging.INFO, "icat_provider_progress": event}))

    text = output.getvalue()
    assert "first · 10/20 games checked (50%)" in text
    assert "second · 0/20 games checked (0%)" in text
    assert "Plan" not in text


def test_rich_uses_one_provider_bar_instead_of_overall_plan_bar(snapshot: ProgressSnapshot) -> None:
    handler = RichProgressHandler(StringIO(), Diagnostics())
    metadata = replace(snapshot.current, step=Step("metadata", "Fetch metadata", "games"), completed=0)
    handler.handle(create_progress_record(replace(snapshot, steps=(metadata,))))
    event = ProviderProgress("first", 1, 2, 5, 10)
    handler.handle(logging.makeLogRecord({"levelno": logging.INFO, "icat_provider_progress": event}))
    output = StringIO()

    Console(file=output, width=100, color_system=None).print(handler.render())

    text = output.getvalue()
    assert "first · 5/10 games checked (50%)" in text
    assert "0/10 games" not in text and "Plan" not in text


def test_shortened_names_do_not_merge_warnings_from_distinct_sources() -> None:
    output = StringIO()
    handler = PlainProgressHandler(output)

    for directory in ("first", "second"):
        handler.handle(logging.makeLogRecord({
            "levelno": logging.WARNING, "msg": "%s — rejected", "args": (Path(f"/source/{directory}/Game.nes"),),
        }))

    handler.flush_repeats()

    assert output.getvalue().count("WARN  \"Game.nes\" — rejected") == 2
    assert "repeated" not in output.getvalue()


def test_completed_with_rejections_does_not_leave_a_successful_live_panel(snapshot: ProgressSnapshot) -> None:
    handler = RichProgressHandler(StringIO(), Diagnostics())
    handler.handle(create_progress_record(replace(snapshot, status="complete_with_rejections")))
    output = StringIO()

    Console(file=output, width=80, color_system=None).print(handler.render())

    assert "complete" not in output.getvalue().lower()
    assert "100%" not in output.getvalue()


def test_plain_fast_work_does_not_print_every_percentage(
    snapshot: ProgressSnapshot, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def get_monotonic_time() -> float:
        return 1

    monkeypatch.setattr("icat.reporting.terminal.monotonic", get_monotonic_time)
    output = StringIO()
    handler = PlainProgressHandler(output)

    for count in range(101):
        current = replace(snapshot.current, completed=count, total=100)
        handler.handle(create_progress_record(replace(snapshot, steps=(current,))))

    handler.handle(create_progress_record(replace(snapshot, status="failed")))

    lines = output.getvalue().splitlines()
    assert len(lines) == 3
    assert "[1/1] Scan" in lines[0] and "0/100" in lines[0]
    assert "confirmed records" in lines[1]
    assert "[1/1] Scan" in lines[2]
