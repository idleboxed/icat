"""Offline progress contracts; counts and timings below are synthetic."""

import logging
from dataclasses import replace
from typing import Any

import pytest

from icat.reporting.journal import RunJournal
from icat.reporting.progress import ProgressSnapshot, RunProgress, Step
from icat.operations.options import Options
from icat.operations.session import run


@pytest.fixture
def progress_records(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.INFO, logger="icat")
    return caplog


def get_snapshots(records: pytest.LogCaptureFixture) -> list[ProgressSnapshot]:
    return [record.icat_progress for record in records.records if hasattr(record, "icat_progress")]


def test_progress_keeps_one_monotonic_plan_and_immutable_snapshots(progress_records: pytest.LogCaptureFixture) -> None:

    with RunProgress((Step("scan", "Scan", "files"), Step("finish", "Finish"))) as progress:
        progress.start("scan")
        progress.set_counts(source_files=2, candidate_sources=2, catalogue_entries=0)
        progress.set_total(2)
        progress.set_item("first.nes")
        progress.advance()
        halfway = get_snapshots(progress_records)[-1]
        progress.advance()
        progress.start("finish")

        assert halfway.current.completed == 1
        assert halfway.current.item is None
        assert halfway.percent == 25
        assert get_snapshots(progress_records)[-1].percent == 50
        assert all(snapshot.percent < 100 for snapshot in get_snapshots(progress_records))

    events = get_snapshots(progress_records)
    assert [event.percent for event in events] == sorted(event.percent for event in events)
    assert events[-1].percent == 100
    assert events[-1].status == "complete"
    assert events[-1].resolved == 2
    assert events[-1].inventory.catalogue_entries == 0
    assert halfway.current.completed == 1


@pytest.mark.parametrize("failure", [OSError("Synthetic failure"), KeyboardInterrupt()])
def test_full_counter_is_not_success_after_failure(
    progress_records: pytest.LogCaptureFixture, failure: BaseException,
) -> None:

    with pytest.raises(type(failure), match="Synthetic failure" if isinstance(failure, OSError) else "^$"):

        with RunProgress((Step("write", "Write"),)) as progress:
            progress.start("write", total=1)
            progress.advance()
            raise failure

    events = get_snapshots(progress_records)
    assert all(event.percent < 100 for event in events)
    assert events[-1].status == ("interrupted" if isinstance(failure, KeyboardInterrupt) else "failed")
    assert events[-1].current.completed == 1


@pytest.mark.parametrize(
    ("action", "arguments", "message"),
    [
        ("start", {"key": "missing"}, "follow the run plan"),
        ("set_total", {"total": -1}, "before counted work"),
        ("advance", {}, "known total"),
        ("set_counts", {"source_files": -1}, "must not be negative"),
    ],
)
def test_invalid_progress_is_rejected(action: str, arguments: dict[str, str | int], message: str) -> None:

    with pytest.raises(ValueError, match=message):

        with RunProgress((Step("scan", "Scan"),)) as progress:
            progress.start("scan")
            getattr(progress, action)(**arguments)


@pytest.mark.parametrize("steps", [(), (Step("same", "A"), Step("same", "B"))])
def test_invalid_plan_is_rejected(steps: tuple[Step, ...]) -> None:

    with pytest.raises(ValueError, match="distinct steps"):
        RunProgress(steps)


@pytest.mark.parametrize("move", [False, True])
@pytest.mark.parametrize("trash_mode", ["none", "trash", "trash_reset"])
def test_sync_plan_accounts_for_options_and_empty_work(
    options: Options, provider: Any, progress_records: pytest.LogCaptureFixture, move: bool, trash_mode: str,
) -> None:
    options = replace(options, move_roms=move, **({trash_mode: True} if trash_mode != "none" else {}))

    result = run(options, provider)

    events = get_snapshots(progress_records)
    final = events[-1]
    assert result.total == 0
    assert len(final.steps) == 6 + move + (trash_mode != "none")
    assert all(len(event.steps) == len(final.steps) for event in events)
    assert final.resolved == len(final.steps)
    assert {step.step.key for step in final.steps if step.status == "not_needed"} >= {
        "scan", "validate", "metadata",
    }
    assert final.inventory.source_files == 0
    assert final.inventory.catalogue_entries == 0
    assert not (options.dst / ".icat.lock").exists()


def test_inventory_tracks_confirmed_index_not_prepared_games(
    options: Options, provider: Any, nes: bytes, progress_records: pytest.LogCaptureFixture,
) -> None:
    (options.src / "game.nes").write_bytes(nes)
    (options.src / "notes.txt").write_text("Synthetic note", encoding="utf-8")

    run(options, provider)

    events = get_snapshots(progress_records)
    assert events[-1].inventory.source_files == 2
    assert events[-1].inventory.candidate_sources == 1
    assert events[-1].inventory.catalogue_entries == 1
    assert all(event.inventory.catalogue_entries == 0 for event in events if event.current.step.key == "metadata")
    assert all(
        event.current.step.key in {"publish", "finalize"}
        for event in events if event.inventory.catalogue_entries == 1
    )
    progress_records.clear()

    run(options, provider)

    assert all(
        event.inventory.catalogue_entries == 1
        for event in get_snapshots(progress_records) if event.current.step.key != "prepare"
    )


@pytest.mark.parametrize("fail", [False, True])
def test_success_waits_for_final_journal_and_resource_cleanup(
    options: Options, provider: Any, nes: bytes, progress_records: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch, fail: bool,
) -> None:
    (options.src / "game.nes").write_bytes(nes)
    original = RunJournal.record
    final_checks = []

    def record(journal: RunJournal, state: str, *, force: bool = False) -> None:

        if force and state == "complete":
            assert all(event.percent < 100 for event in get_snapshots(progress_records))
            assert not (options.dst / ".icat.lock").exists()
            assert not list(options.dst.glob(".icat-stage-*"))
            final_checks.append(state)

            if fail:
                raise OSError("Synthetic final checkpoint failure")

        return original(journal, state, force=force)

    monkeypatch.setattr(RunJournal, "record", record)

    if fail:

        with pytest.raises(OSError, match="final checkpoint failure"):
            run(options, provider)

    else:
        run(options, provider)

    assert final_checks == ["complete"]
    assert get_snapshots(progress_records)[-1].status == ("failed" if fail else "complete")
    assert any(record.getMessage().startswith("COMPLETED") for record in progress_records.records) is not fail


def test_rejected_source_is_processed_but_not_counted_as_a_catalogue_record(
    options: Options, provider: Any, progress_records: pytest.LogCaptureFixture,
) -> None:
    (options.src / "bad.nes").write_bytes(b"not a ROM")

    result = run(options, provider)

    final = get_snapshots(progress_records)[-1]
    assert result.rejected == 1
    assert final.percent == 100
    assert final.status == "complete_with_rejections"
    assert final.inventory.rejected_sources == 1
    assert final.inventory.catalogue_entries == 0
    scan = next(step for step in final.steps if step.step.key == "scan")
    assert scan.completed == scan.total == 1
