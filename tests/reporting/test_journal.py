import errno
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from icat.errors import CatalogueError
from icat.reporting.diagnostics import Diagnostics
from icat.io.files import write_atomic, sync_directory
from icat.reporting.journal import RunJournal


def test_identical_start_time_never_overwrites_an_existing_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seconds = int(datetime(2026, 9, 19, 12, 34, 56, tzinfo=UTC).timestamp())

    def get_iso_time() -> str:
        return "2026-09-19T12:34:56.123456Z"

    def get_time() -> int:
        return seconds

    monkeypatch.setattr("icat.reporting.journal.get_iso_time", get_iso_time)
    monkeypatch.setattr("icat.reporting.journal.time", get_time)

    with RunJournal(tmp_path, {}, Diagnostics()) as first:
        first.record("complete")

    before = first.path.read_bytes()

    with RunJournal(tmp_path, {}, Diagnostics()) as second:
        second.record("complete")

    assert first.path.parent.name == "2026-09-19T12:34:56Z"
    assert second.path.parent.name == "2026-09-19T12:34:56Z_01"
    assert first.path.name == second.path.name == "journal.json"
    assert first.path.read_bytes() == before


def test_parallel_routine_diagnostics_keep_counts_without_per_rom_history(tmp_path: Path) -> None:
    diagnostics = Diagnostics(event_limit=5)

    def emit(number: int) -> None:

        with diagnostics.capture_events(f"source{number}", f"{number}"):

            for _request in range(10):
                diagnostics.emit("http", "response", host="example.test", sent=True, status=200)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(emit, range(4)))

    with RunJournal(tmp_path, {}, diagnostics) as journal:
        journal.record("complete")

    doc = json.loads(journal.path.read_bytes())["diagnostics"]
    assert doc["events"] == [] and doc["events_omitted"] == 0
    assert doc["hosts"]["example.test"]["requests_sent"] == 40
    assert all(counts["http.response"] == 10 for counts in doc["counts"].values())


def test_grouped_failure_details_survive_the_detailed_event_limit() -> None:
    diagnostics = Diagnostics(event_limit=1)

    with diagnostics.capture_events("hasheous", "first-rom"):
        diagnostics.emit("http", "response", host="hasheous.org", sent=True, status=200)
        diagnostics.emit("issue", "source_error", reason="http_status", status=429)

    with diagnostics.capture_events("hasheous", "last-rom"):
        diagnostics.emit("issue", "source_error", reason="http_status", status=429)

    result = diagnostics.get_snapshot()

    assert result["events"] == [] and result["events_omitted"] == 0
    assert result["issues"][0]["count"] == 2
    assert result["issues"][0]["first"]["rom"] == "first-rom"
    assert result["issues"][0]["last"]["rom"] == "last-rom"
    assert result["issues"][0]["last"]["status"] == 429


def test_diagnostics_keep_both_start_and_recent_events_within_one_budget() -> None:
    diagnostics = Diagnostics(event_limit=6)

    for number in range(20):
        diagnostics.emit("source", "archive_selection", number=number)

    result = diagnostics.get_snapshot()

    assert [event["number"] for event in result["events"]] == [0, 1, 2, 17, 18, 19]
    assert result["events_prefix_count"] == result["events_tail_count"] == 3
    assert result["events_omitted"] == 14
    assert result["counts"]["transport"]["source.archive_selection"] == 20


def test_network_failure_reasons_are_deduplicated_across_roms_and_requests() -> None:
    diagnostics = Diagnostics(event_limit=0)

    for number in range(40):

        with diagnostics.capture_events("hasheous", f"rom-{number}"):
            diagnostics.emit(
                "http", "failure", host="hasheous.org", reason="ReadTimeout", status=None,
                sent=True, request=f"request-{number}",
            )
            diagnostics.emit("http", "retry_scheduled", host="hasheous.org", reason="ReadTimeout", sent=False)
            diagnostics.emit("http", "response", host="hasheous.org", status=200, sent=True)

    result = diagnostics.get_snapshot()

    assert result["events"] == []
    assert result["events_omitted"] == 0
    assert len(result["network_failures"]) == 1
    failure = result["network_failures"][0]
    assert failure["count"] == 40
    assert (failure["source"], failure["host"], failure["reason"], failure["status"]) == (
        "hasheous", "hasheous.org", "ReadTimeout", None,
    )
    assert failure["first"]["rom"] == "rom-0"
    assert failure["last"]["rom"] == "rom-39"
    assert failure["last"]["request"] == "request-39"
    assert result["hosts"]["hasheous.org"]["requests_sent"] == 80
    assert result["network_failures_omitted"] == 0


def test_network_failures_separate_hosts_sources_reasons_and_statuses() -> None:
    diagnostics = Diagnostics(event_limit=0)
    cases = [
        ("a", "first.test", "ReadTimeout", None),
        ("b", "first.test", "ReadTimeout", None),
        ("a", "second.test", "ReadTimeout", None),
        ("a", "first.test", "ConnectionError", None),
        ("a", "first.test", "http_status", 429),
        ("a", "first.test", "http_status", 503),
    ]

    for source, host, reason, status in cases:

        with diagnostics.capture_events(source, "rom"):
            diagnostics.emit(
                "http", "failure" if status is None else "response", host=host,
                reason=reason, status=status, sent=True,
            )

    diagnostics.emit("http", "response", host="first.test", status=404, sent=True)

    result = diagnostics.get_snapshot()

    assert len(result["network_failures"]) == len(cases)
    assert all(event["count"] == 1 for event in result["network_failures"])


def test_group_limit_is_explicit_and_existing_failure_counts_keep_growing() -> None:
    diagnostics = Diagnostics(event_limit=0)

    for number in range(257):
        diagnostics.emit("http", "failure", host=f"host-{number}.test", reason="Timeout", sent=True)
        diagnostics.emit("issue", "source_error", reason=f"reason-{number}")

    diagnostics.emit("http", "failure", host="host-0.test", reason="Timeout", sent=True)

    result = diagnostics.get_snapshot()

    assert len(result["network_failures"]) == len(result["issues"]) == 256
    assert result["network_failures"][0]["count"] == 2
    assert result["network_failures_omitted"] == result["issues_omitted"] == 1
    diagnostics.reset()
    result = diagnostics.get_snapshot()
    assert result["network_failures"] == result["issues"] == result["events"] == []
    assert result["network_failures_omitted"] == result["issues_omitted"] == result["events_omitted"] == 0


def test_routine_noise_cannot_evict_useful_events_or_duplicate_failure_details() -> None:
    diagnostics = Diagnostics(event_limit=1)

    with diagnostics.capture_events("fixture", "rom") as local:
        diagnostics.emit("source", "nes_trailing_data", bytes=8192)

        for _request in range(100):
            diagnostics.emit("http", "cache_hit", host="example.test", sent=False)
            diagnostics.emit("lookup", "missing_key")
            diagnostics.emit("thumbnail", "not_listed", name="Game.png")

        diagnostics.emit("http", "failure", host="example.test", reason="ReadTimeout", sent=True)
        diagnostics.emit("issue", "source_error", reason="ReadTimeout", status=None)

    doc = diagnostics.get_snapshot()

    assert [event["outcome"] for event in doc["events"]] == ["nes_trailing_data"]
    assert doc["events_omitted"] == 0
    assert doc["counts"]["fixture"]["lookup.missing_key"] == 100
    assert len(doc["network_failures"]) == 1
    assert "last" not in doc["network_failures"][0]
    assert doc["issues"] == []
    assert any(event["kind"] == "issue" for event in local)


def test_change_provenance_deduplicates_sanitized_urls(tmp_path: Path) -> None:

    with RunJournal(tmp_path, {}, Diagnostics()) as journal:
        journal.record_metadata_changes("rom", {"title": "fixture"}, ["https://example.test/data?key=secret"])
        journal.record_metadata_changes("rom", {"thumbnail": "fixture"}, ["https://example.test/data?key=other"])
        journal.data["state"] = "complete"

    doc = json.loads(journal.path.read_bytes())

    assert doc["metadata_fields"] == {"rom": {"title": "fixture", "thumbnail": "fixture"}}
    assert doc["metadata_sources"] == {"rom": ["s1"]}
    assert doc["source_urls"] == {"s1": "https://example.test/data"}


def test_provenance_keeps_numeric_mapping_pairs_separate_from_secret_urls(tmp_path: Path) -> None:

    with RunJournal(tmp_path, {}, Diagnostics()) as journal:

        for digest, game_id in (("first", "00042"), ("second", "43")):
            journal.record_metadata_changes(
                digest, {"year": "thegamesdb"}, [f"https://thegamesdb.net/game.php?id={game_id}&apikey=secret"],
                identifiers={
                    "TheGamesDb:game": game_id, "TheGamesDb:platform": "07", "title": "secret", "token": "secret",
                },
            )

        journal.record_metadata_changes(
            "unchanged", {}, identifiers={"TheGamesDb:game": "44", "TheGamesDb:platform": "7"},
        )
        journal.data["state"] = "complete"

    doc = json.loads(journal.path.read_bytes())

    assert doc["metadata_identifiers"] == {
        "first": {"TheGamesDb:game": "42", "TheGamesDb:platform": "7"},
        "second": {"TheGamesDb:game": "43", "TheGamesDb:platform": "7"},
    }
    assert doc["source_urls"] == {"s1": "https://thegamesdb.net/game.php"}
    assert "secret" not in journal.path.read_text()


@pytest.mark.parametrize("invalid", [None, "", "0", "-1", "9" * 13, "42&key=secret", "１２", True, 7, []])
@pytest.mark.parametrize("name", ["TheGamesDb:game", "TheGamesDb:platform"])
def test_provenance_rejects_incomplete_or_invalid_mapping_pairs(tmp_path: Path, name: str, invalid: Any) -> None:
    identifiers = {"TheGamesDb:game": "42", "TheGamesDb:platform": "7", name: invalid}

    with RunJournal(tmp_path, {}, Diagnostics()) as journal:
        journal.record_metadata_changes("rom", {"year": "fixture"}, identifiers=identifiers)
        journal.data["state"] = "complete"

    assert json.loads(journal.path.read_bytes())["metadata_identifiers"] == {}


def test_unknown_genres_keep_separate_counts_and_examples_without_per_rom_history() -> None:
    diagnostics = Diagnostics(event_limit=0)

    for source, rom, genre in (
        ("thegamesdb", "a", "Horror"), ("thegamesdb", "b", "Life Simulation"),
        ("thegamesdb", "c", "Horror"), ("other", "d", "Horror"),
    ):

        with diagnostics.capture_events(source, rom):
            diagnostics.emit("issue", "unknown_genre", genre=genre)

    result = diagnostics.get_snapshot()

    groups = {(group["first"]["source"], group["first"]["genre"]): group for group in result["issues"]}
    assert len(groups) == 3
    assert groups["thegamesdb", "Horror"]["count"] == 2
    assert groups["thegamesdb", "Horror"]["first"]["rom"] == "a"
    assert groups["thegamesdb", "Horror"]["last"]["rom"] == "c"
    assert groups["thegamesdb", "Life Simulation"]["count"] == 1
    assert groups["other", "Horror"]["count"] == 1
    assert result["counts"]["thegamesdb"]["issue.unknown_genre"] == 3
    assert result["events"] == []


def test_distinct_unknown_genres_obey_the_shared_issue_group_budget() -> None:
    diagnostics = Diagnostics(event_limit=0)

    for number in range(257):
        diagnostics.emit("issue", "unknown_genre", genre=f"Synthetic genre {number}")

    diagnostics.emit("issue", "unknown_genre", genre="Synthetic genre 0")

    result = diagnostics.get_snapshot()

    assert len(result["issues"]) == 256
    assert result["issues"][0]["count"] == 2
    assert result["issues_omitted"] == 1


def test_large_unchanged_metadata_run_has_constant_size_diagnostics(tmp_path: Path) -> None:
    diagnostics = Diagnostics()

    with RunJournal(tmp_path, {}, diagnostics) as journal:

        for number in range(1646):

            with diagnostics.capture_events("fixture", f"{number}"):
                diagnostics.emit("http", "cache_hit", host="example.test", request=f"{number}", sent=False)
                diagnostics.emit("lookup", "matched", fields=[], matched=True)
                diagnostics.emit("lookup", "missing_key")
                diagnostics.emit("thumbnail", "not_listed", name=f"Game {number}.png")

        journal.data["state"] = "complete"

    doc = json.loads(journal.path.read_bytes())

    assert doc["diagnostics"]["events"] == []
    assert doc["diagnostics"]["counts"]["fixture"]["lookup.matched"] == 1646
    assert journal.path.stat().st_size < 2500


def test_metadata_checkpoints_use_small_periodic_jsonl_and_final_json_is_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]

    def get_monotonic_time() -> float:
        return now[0]

    monkeypatch.setattr("icat.reporting.journal.monotonic", get_monotonic_time)

    with RunJournal(tmp_path, {}, Diagnostics()) as journal:
        baseline = journal.path.read_bytes()
        journal.record("metadata")

        for count in range(1, 100):
            journal.data["metadata_completed"] = count
            journal.record("metadata")

        assert journal.path.read_bytes() == baseline
        assert len(journal.progress_path.read_text().splitlines()) == 1
        now[0] = 300.0
        journal.record("metadata")
        checkpoints = [json.loads(line) for line in journal.progress_path.read_text().splitlines()]
        assert checkpoints[-1]["metadata_completed"] == 99
        journal.data["metadata_completed"] = 100
        journal.record("publishing")
        checkpoints = [json.loads(line) for line in journal.progress_path.read_text().splitlines()]
        assert checkpoints[-1]["state"] == "publishing"
        assert checkpoints[-1]["metadata_completed"] == 100
        assert journal.path.read_bytes() == baseline
        journal.data["state"] = "complete"

    doc = json.loads(journal.path.read_bytes())
    assert doc["state"] == "complete"
    assert doc["finished_at"]
    assert doc["progress_log"] == "progress.jsonl"


def test_removals_append_small_durable_records_instead_of_rewriting_full_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes = []

    def write_snapshot(path: Path, data: bytes) -> None:
        writes.append(len(data))
        write_atomic(path, data)

    monkeypatch.setattr("icat.reporting.journal.write_atomic", write_snapshot)

    with RunJournal(tmp_path, {}, Diagnostics()) as journal:
        journal.data["sources"] = [{"path": f"{tmp_path / f"{number}"}", "sha256": "a" * 64} for number in range(200)]

        for number in range(200):

            with journal.track_removal(tmp_path / f"{number}", collection="removed", digest="a" * 64):
                events = journal.removal_path.read_text().splitlines()
                assert json.loads(events[-1])["phase"] == "intent"

        events = [json.loads(line) for line in journal.removal_path.read_text().splitlines()]
        assert len(events) == 400
        assert all(events[index]["phase"] == ("intent" if index % 2 == 0 else "complete") for index in range(400))
        assert journal.removal_path.stat().st_size < 200 * 600
        assert len(writes) == 2
        journal.data["state"] = "complete"

    doc = json.loads(journal.path.read_bytes())
    assert len(doc["removed"]) == 200
    assert "pending_removal" not in doc and "removal_log" not in doc
    assert not journal.removal_path.exists()
    assert len(writes) == 3


@pytest.mark.parametrize("phase", ["intent", "complete"])
@pytest.mark.parametrize("collection", ["removed", "trash_removed"])
def test_removal_sync_failure_stops_and_preserves_recovery_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, collection: str,
) -> None:
    source = tmp_path / "input.rom"
    source.write_bytes(b"synthetic")
    journal = RunJournal(tmp_path, {}, Diagnostics())
    original = os.fsync
    calls = []

    def fail_sync(fd: int) -> None:

        if journal.removal_path and journal.removal_path.exists():

            if os.fstat(fd).st_ino == journal.removal_path.stat().st_ino:
                calls.append(fd)

                if len(calls) == (1 if phase == "intent" else 2):
                    raise OSError("Synthetic journal sync failure")

        original(fd)

    monkeypatch.setattr("icat.reporting.journal.os.fsync", fail_sync)

    with pytest.raises(OSError, match="Synthetic journal sync failure"), journal:

        with journal.track_removal(source, collection=collection, digest="a" * 64):
            source.unlink()
            sync_directory(source.parent)

    doc = json.loads(journal.path.read_bytes())
    assert doc["state"] == "failed"
    assert doc[collection] == []
    assert doc["pending_removal"]["collection"] == collection
    assert doc["pending_removal"]["path"] == f"{source}"
    assert doc["removal_log"] == journal.removal_path.name
    assert source.exists() is (phase == "intent")
    assert len(calls) == (1 if phase == "intent" else 2)
    assert journal.removal_path.exists()


@pytest.mark.parametrize("failure", [OSError("Synthetic unlink failure"), KeyboardInterrupt()])
def test_failed_or_cancelled_removal_keeps_intent_and_does_not_mark_completion(
    tmp_path: Path, failure: BaseException,
) -> None:

    with (
        pytest.raises(type(failure), match="Synthetic unlink failure" if isinstance(failure, OSError) else "^$"),
        RunJournal(tmp_path, {}, Diagnostics()) as journal,
    ):

        with journal.track_removal(tmp_path / "input.rom", collection="trash_removed", digest="a" * 64):
            raise failure

    doc = json.loads(journal.path.read_bytes())
    assert doc["state"] == ("interrupted" if isinstance(failure, KeyboardInterrupt) else "failed")
    assert doc["trash_removed"] == []
    assert doc["pending_removal"]["collection"] == "trash_removed"
    assert len(journal.removal_path.read_text().splitlines()) == 1


def test_caught_removal_error_cannot_be_followed_by_another_removal_or_success(tmp_path: Path) -> None:

    with pytest.raises(CatalogueError, match="operation cannot be completed"):

        with RunJournal(tmp_path, {}, Diagnostics()) as journal:

            with pytest.raises(OSError, match="Synthetic"):

                with journal.track_removal(tmp_path / "a", collection="removed", digest="a" * 64):
                    raise OSError("Synthetic unlink failure")

            with pytest.raises(CatalogueError, match="refusing further removals"):

                with journal.track_removal(tmp_path / "b", collection="removed", digest="b" * 64):
                    pytest.fail("Must not reach another destructive operation")

    assert json.loads(journal.path.read_bytes())["state"] == "failed"
    assert len(journal.removal_path.read_text().splitlines()) == 1


def test_failed_final_snapshot_does_not_delete_the_removal_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def write_snapshot(path: Path, data: bytes) -> None:

        if "finished_at" in json.loads(data):
            raise OSError("Synthetic final snapshot failure")

        write_atomic(path, data)

    monkeypatch.setattr("icat.reporting.journal.write_atomic", write_snapshot)

    with pytest.raises(OSError, match="final snapshot failure"), RunJournal(tmp_path, {}, Diagnostics()) as journal:

        with journal.track_removal(tmp_path / "a", collection="removed", digest="a" * 64):
            pass

        journal.data["state"] = "complete"

    assert journal.removal_path.exists()
    assert len(journal.removal_path.read_text().splitlines()) == 2
    assert "finished_at" not in json.loads(journal.path.read_bytes())


def test_incomplete_stage_does_not_compact_its_removal_log(tmp_path: Path) -> None:

    with RunJournal(tmp_path, {}, Diagnostics()) as journal:

        with journal.track_removal(tmp_path / "a", collection="removed", digest="a" * 64):
            pass

    doc = json.loads(journal.path.read_bytes())
    assert doc["state"] == "removing_sources"
    assert journal.removal_path.exists()
    assert doc["removal_log"] == journal.removal_path.name


def test_existing_removal_log_is_never_overwritten(tmp_path: Path) -> None:

    with (
        pytest.raises(FileExistsError, match=rf"\[Errno {errno.EEXIST}\].*removals\.jsonl"),
        RunJournal(tmp_path, {}, Diagnostics()) as journal,
    ):
        recovery = journal.path.with_name("removals.jsonl")
        recovery.write_bytes(b"previous recovery data\n")

        with journal.track_removal(tmp_path / "a", collection="removed", digest="a" * 64):
            pytest.fail("A preexisting recovery log must block removal")

    assert recovery.read_bytes() == b"previous recovery data\n"
    assert json.loads(journal.path.read_bytes())["state"] == "failed"


def test_abrupt_process_exit_leaves_a_durable_removal_intent(tmp_path: Path) -> None:
    script = """
import os
import sys
from pathlib import Path
from icat.reporting.diagnostics import Diagnostics
from icat.reporting.journal import RunJournal

with RunJournal(Path(sys.argv[1]), {}, Diagnostics()) as journal:

    with journal.track_removal(Path(sys.argv[1]) / "input.rom", collection="removed", digest="a" * 64):
        os._exit(23)
"""

    result = subprocess.run([sys.executable, "-c", script, f"{tmp_path}"], check=False, timeout=10)

    assert result.returncode == 23
    journal_path = next(tmp_path.rglob("journal.json"))
    doc = json.loads(journal_path.read_bytes())
    assert doc["state"] == "removing_sources"
    assert "finished_at" not in doc
    removal_path = journal_path.parent / doc["removal_log"]
    events = [json.loads(line) for line in removal_path.read_text().splitlines()]
    assert len(events) == 1
    assert events[0]["phase"] == "intent"
    assert events[0]["sha256"] == "a" * 64
