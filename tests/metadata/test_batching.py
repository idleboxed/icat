"""Synthetic batch contracts and sync integration, without network access.

SPDX-License-Identifier: BSD-3-Clause
"""

import json
import logging
from collections.abc import Callable, Sequence
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from icat.databases.cache import DatasetCache
from icat.metadata.provider import Provider, build_provider
from icat.io.http import HttpClient
from icat.roms.types import Rom
from icat.metadata.types import LookupContext, Metadata
from icat.metadata.sources.base import MetadataSource
from icat.operations.options import Options
from icat.operations.session import run


@pytest.fixture
def contexts(rom: Rom) -> list[LookupContext]:
    result = []

    for index in range(5):
        entry = {**rom.entry, "hash": f"{index:064x}", "image": f"Game-{index}.nes", "title": f"Game-{index}"}
        result.append(LookupContext(replace(rom, entry=entry), dict(entry)))

    return result


@pytest.mark.parametrize(("environment", "size"), [({}, 1), ({"ICAT_THEGAMESDB_API_KEY": "synthetic-key"}, 20)])
def test_only_available_native_sources_affect_provider_batch_size(
    tmp_path: Path, environment: dict[str, str], size: int,
) -> None:
    provider = build_provider(HttpClient(tmp_path), environ=environment)

    assert provider.batch_size == provider.get_settings()["metadata_batch_size"] == size


def test_missing_batch_credentials_and_empty_input_do_not_fetch(
    tmp_path: Path, contexts: list[LookupContext],
) -> None:
    class Keyed(MetadataSource):
        provides = frozenset({"year"})
        batch_size = 20
        key_env = "SYNTHETIC_KEY"

        def fetch(self, context: LookupContext) -> Metadata:
            raise AssertionError("No key")

    source = Keyed(HttpClient(tmp_path), DatasetCache(tmp_path / "db"))
    original_paths = set(tmp_path.iterdir())

    assert source.lookup_many([]) == []
    assert all(result.outcome == "missing_key" for result in source.lookup_many(contexts))
    assert set(tmp_path.iterdir()) == original_paths


@pytest.mark.parametrize("size", [0, 129, True, "20"])
def test_invalid_batch_size_is_rejected_before_fetch(
    tmp_path: Path, contexts: list[LookupContext], size: Any,
) -> None:
    class Invalid(MetadataSource):
        provides = frozenset({"year"})
        batch_size = size

        def fetch(self, context: LookupContext) -> Metadata:
            raise AssertionError

    source = Invalid(HttpClient(tmp_path), DatasetCache(tmp_path / "db"))

    with pytest.raises(ValueError, match="batch size"):
        source.lookup_many(contexts)

    with pytest.raises(ValueError, match="batch size"):
        _ = Provider([source]).batch_size


def test_single_sources_fall_back_in_order_and_isolate_content_errors(
    tmp_path: Path, contexts: list[LookupContext],
) -> None:
    seen = []

    class Single(MetadataSource):
        name = "single"
        provides = frozenset({"year"})

        def fetch(self, context: LookupContext) -> Metadata:
            seen.append(context.rom.entry["hash"])

            if context is contexts[1]:
                raise ValueError("Malformed remote row")

            return Metadata(fields={"year": 1991})

    source = Single(HttpClient(tmp_path), DatasetCache(tmp_path / "db"))

    results = source.lookup_many(contexts)

    assert seen == [context.rom.entry["hash"] for context in contexts]
    assert [result.fields for result in results] == [{"year": 1991}, {}, *[{"year": 1991}] * 3]
    assert results[1].outcome == "unavailable"


def test_native_batches_filter_filled_fields_and_validate_each_result(
    tmp_path: Path, contexts: list[LookupContext],
) -> None:
    groups = []
    contexts[1].values.update(year=1999, players=4)

    class Native(MetadataSource):
        name = "native"
        provides = frozenset({"year", "players"})
        batch_size = 2

        def fetch(self, context: LookupContext) -> Metadata:
            raise AssertionError("Native batch path must be used")

        def fetch_many(self, group: Sequence[LookupContext]) -> list[Metadata]:
            groups.append(list(group))
            return [
                Metadata(fields={"year": 1991, "players": -1 if context is contexts[2] else 2}) for context in group
            ]

    source = Native(HttpClient(tmp_path), DatasetCache(tmp_path / "db"))

    results = source.lookup_many(contexts)

    assert groups == [[contexts[0], contexts[2]], [contexts[3], contexts[4]]]
    assert results[1].fields == {} and results[1].outcome == "already_filled"
    assert results[2].fields == {"year": 1991} and results[2].outcome == "partial"
    assert results[0].fields == {"year": 1991, "players": 2}
    assert contexts[1].values["year"] == 1999
    diagnostics = source.http.diagnostics.get_snapshot()
    assert diagnostics["events"] == []
    counts = diagnostics["counts"][source.name]
    assert counts["batch.lookup"] == 2
    assert sum(count for key, count in counts.items() if key.startswith("lookup.")) == 5


@pytest.mark.parametrize("returned", [[], [None], None])
def test_invalid_native_result_contract_propagates_programming_error(
    tmp_path: Path, contexts: list[LookupContext], returned: Any,
) -> None:
    class Broken(MetadataSource):
        provides = frozenset({"year"})
        batch_size = 2

        def fetch(self, context: LookupContext) -> Metadata:
            raise AssertionError

        def fetch_many(self, group: Sequence[LookupContext]) -> Any:
            return returned

    source = Broken(HttpClient(tmp_path), DatasetCache(tmp_path / "db"))

    with pytest.raises(TypeError, match="Metadata"):
        source.lookup_many(contexts[:1])


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), TypeError("source bug")])
def test_cancel_and_programming_errors_stop_batches(
    tmp_path: Path, contexts: list[LookupContext], failure: BaseException,
) -> None:
    seen = []

    class Broken(MetadataSource):
        provides = frozenset({"year"})

        def fetch(self, context: LookupContext) -> Metadata:
            seen.append(context)
            raise failure

    source = Broken(HttpClient(tmp_path), DatasetCache(tmp_path / "db"))

    with pytest.raises(type(failure), match="source bug" if isinstance(failure, TypeError) else "^$"):
        source.lookup_many(contexts)

    assert seen == contexts[:1]


def test_provider_batches_preserve_precedence_ids_placeholder_fallback_and_input_entries(
    tmp_path: Path, contexts: list[LookupContext],
) -> None:
    later = []

    class First(MetadataSource):
        name = "first"
        provides = frozenset({"title", "year", "genre"})
        identifies = True

        def fetch(self, context: LookupContext) -> Metadata:

            if context.rom.entry["hash"] == contexts[0].rom.entry["hash"]:
                return Metadata(fields={"title": "ZZZ"})

            return Metadata(fields={"year": 1991, "genre": "Other"}, identifiers={"test:game": "42"})

    class Second(First):
        name = "second"
        batch_size = 2
        provides = frozenset({"year", "genre", "players"})

        def fetch_many(self, group: Sequence[LookupContext]) -> list[Metadata]:
            later.extend(context.rom.entry["hash"] for context in group)

            for context in group:
                expected = {} if context.rom is contexts[0].rom else {"test:game": "42"}
                assert context.identifiers == expected

            return [Metadata(fields={"year": 2000, "genre": "Sports", "players": 2}) for _context in group]

    http, cache = HttpClient(tmp_path), DatasetCache(tmp_path / "db")
    provider = Provider([First(http, cache), Second(http, cache)])
    roms = [context.rom for context in contexts]
    before = deepcopy([rom.entry for rom in roms])

    results = provider.lookup_many(roms)

    assert results[0].fields == {"year": 2000, "genre": "Sports", "players": 2}
    assert results[0].identifiers == {}
    assert later == [rom.entry["hash"] for rom in roms]
    assert all(result.fields == {"year": 1991, "genre": "Sports", "players": 2} for result in results[1:])
    assert results[1].field_sources == {"year": "first", "genre": "second", "players": "second"}
    assert all(result.identifiers == {"test:game": "42"} for result in results[1:])
    assert [rom.entry for rom in roms] == before
    assert provider.batch_size == 2


def test_batch_error_does_not_block_later_batches_or_fallback_sources(
    tmp_path: Path, contexts: list[LookupContext],
) -> None:
    class First(MetadataSource):
        name = "first"
        provides = frozenset({"year"})
        batch_size = 2

        def fetch(self, context: LookupContext) -> Metadata:
            return Metadata(fields={"year": 1992})

        def fetch_many(self, group: Sequence[LookupContext]) -> list[Metadata]:

            if group[0].rom.entry["hash"] == contexts[0].rom.entry["hash"]:
                raise ValueError("Invalid first batch")

            return super().fetch_many(group)

    class Fallback(First):
        name = "fallback"
        batch_size = 1

        def fetch_many(self, group: Sequence[LookupContext]) -> list[Metadata]:
            return [Metadata(fields={"year": 1993}) for _context in group]

    http, cache = HttpClient(tmp_path), DatasetCache(tmp_path / "db")

    results = Provider([First(http, cache), Fallback(http, cache)]).lookup_many([context.rom for context in contexts])

    assert [result.fields["year"] for result in results] == [1993, 1993, 1992, 1992, 1992]


def test_sync_uses_native_batches_and_keeps_per_rom_progress(options: Options, nes: bytes) -> None:
    sizes = []

    class Native(MetadataSource):
        name = "native"
        provides = frozenset({"year"})
        batch_size = 20

        def fetch(self, context: LookupContext) -> Metadata:
            raise AssertionError

        def fetch_many(self, group: Sequence[LookupContext]) -> list[Metadata]:
            sizes.append(len(group))
            return [Metadata(fields={"year": 1991}) for _context in group]

    for index in range(21):
        (options.src / f"Game-{index:02}.nes").write_bytes(nes[:-1] + bytes([index]))

    http = HttpClient(options.cache)
    provider = Provider([Native(http, DatasetCache(options.cache / "db"))])

    result = run(replace(options, concurrency=1), provider)

    assert result.imported == result.total == 21
    assert sizes == [20, 1]
    journal = json.loads(next(options.logs.rglob("journal.json")).read_text())
    assert journal["metadata_completed"] == 21
    assert len(journal["metadata_fields"]) == 21
    assert journal["summary"]["missing"]["year"] == 0


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), TypeError("batch implementation bug")])
def test_sync_batch_cancel_or_bug_does_not_publish_or_remove_sources(
    options: Options, nes: bytes, failure: BaseException,
) -> None:
    class Broken(MetadataSource):
        name = "broken"
        provides = frozenset({"year"})
        batch_size = 2

        def fetch(self, context: LookupContext) -> Metadata:
            raise failure

    for index in range(3):
        (options.src / f"Game-{index}.nes").write_bytes(nes[:-1] + bytes([index]))

    original = {path: path.read_bytes() for path in options.src.iterdir()}
    http = HttpClient(options.cache)
    provider = Provider([Broken(http, DatasetCache(options.cache / "db"))])

    with pytest.raises(
        type(failure), match="batch implementation bug" if isinstance(failure, TypeError) else "^$",
    ):
        run(replace(options, concurrency=1, move_roms=True), provider)

    assert all(path.read_bytes() == data for path, data in original.items())
    assert not (options.dst / "catalogue.json").exists()
    assert not (options.dst / ".icat.lock").exists()
    assert http.cancelled.is_set()
    journal = json.loads(next(options.logs.rglob("journal.json")).read_text())
    assert journal["failed_stage"] == "metadata"
    assert journal["state"] == ("interrupted" if isinstance(failure, KeyboardInterrupt) else "failed")


def test_sync_packs_after_previous_sources_have_filled_scattered_records(options: Options, nes: bytes) -> None:
    batches = []

    class Earlier(MetadataSource):
        name = "earlier"
        provides = frozenset({"year"})

        def fetch(self, context: LookupContext) -> Metadata:
            index = int(Path(context.rom.entry["image"]).stem.split("-")[1])
            return Metadata(fields={"year": 1990}) if index % 8 else Metadata()

    class Later(MetadataSource):
        name = "later"
        provides = frozenset({"year"})
        batch_size = 20

        def fetch(self, context: LookupContext) -> Metadata:
            raise AssertionError("Expected the packed native batch")

        def fetch_many(self, group: Sequence[LookupContext]) -> list[Metadata]:
            batches.append([context.rom.entry["image"] for context in group])
            return [Metadata(fields={"year": 1991}) for _context in group]

    for index in range(65):
        (options.src / f"Game-{index:02}.nes").write_bytes(nes[:-1] + bytes([index]))

    http, cache = HttpClient(options.cache), DatasetCache(options.cache / "db")
    provider = Provider([Earlier(http, cache), Later(http, cache)])

    result = run(options, provider)

    assert result.imported == result.total == 65
    assert len(batches) == 1 and len(batches[0]) == 9
    journal = json.loads(next(options.logs.rglob("journal.json")).read_bytes())
    assert journal["metadata_completed"] == 65
    assert journal["summary"]["missing"]["year"] == 0


class ImmediateExecutor(Executor):
    """Deterministic completed futures, exposing how much work is submitted before yielding."""

    def __init__(self) -> None:
        self.submitted = 0

    def submit(self, function: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        self.submitted += 1
        future = Future()
        try:
            future.set_result(function(*args, **kwargs))

        except BaseException as exc:
            future.set_exception(exc)

        return future


@pytest.mark.parametrize("workers", [1, 3])
def test_closing_metadata_iterator_cancels_without_submitting_remainder(
    tmp_path: Path, contexts: list[LookupContext], workers: int,
) -> None:
    class Source(MetadataSource):
        provides = frozenset({"year"})

        def fetch(self, context: LookupContext) -> Metadata:
            return Metadata(fields={"year": 1990})

    http = HttpClient(tmp_path)
    provider = Provider([Source(http, DatasetCache(tmp_path / "db"))])
    executor = ImmediateExecutor()

    iterator = provider.iter_lookup([context.rom for context in contexts], executor=executor, max_pending=workers)

    with closing(iterator) as results:
        next(results)
        assert executor.submitted == workers

    assert http.cancelled.is_set()
    assert executor.submitted == workers


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), TypeError("batch bug")])
def test_failed_batch_does_not_replenish_queue_or_start_later_sources(
    tmp_path: Path, contexts: list[LookupContext], failure: BaseException,
) -> None:
    class Broken(MetadataSource):
        provides = frozenset({"year"})

        def fetch(self, context: LookupContext) -> Metadata:
            raise failure

    class Later(Broken):
        def fetch(self, context: LookupContext) -> Metadata:
            raise AssertionError("Later source must not start")

    http, cache = HttpClient(tmp_path), DatasetCache(tmp_path / "db")
    provider = Provider([Broken(http, cache), Later(http, cache)])
    executor = ImmediateExecutor()

    with pytest.raises(type(failure), match="batch bug" if isinstance(failure, TypeError) else "^$"):
        list(provider.iter_lookup([context.rom for context in contexts], executor=executor, max_pending=2))

    assert executor.submitted == 2
    assert http.cancelled.is_set()


def test_parallel_source_passes_preserve_ids_fields_placeholder_fallback_and_result_indexes(
    tmp_path: Path, contexts: list[LookupContext],
) -> None:
    class First(MetadataSource):
        name = "first"
        identifies = True
        provides = frozenset({"title", "year"})

        def fetch(self, context: LookupContext) -> Metadata:

            if context.rom is contexts[0].rom:
                return Metadata(fields={"title": "ZZZ"})

            return Metadata(fields={"year": 1990}, identifiers={"test:game": "5"})

    class Last(MetadataSource):
        name = "last"
        provides = frozenset({"players", "year"})
        batch_size = 2

        def fetch(self, context: LookupContext) -> Metadata:
            expected = {} if context.rom is contexts[0].rom else {"test:game": "5"}
            assert context.identifiers == expected
            return Metadata(fields={"players": 2, "year": 2000})

    http, cache = HttpClient(tmp_path), DatasetCache(tmp_path / "db")
    provider = Provider([First(http, cache), Last(http, cache)])

    with ThreadPoolExecutor(max_workers=3) as executor:
        results = dict(provider.iter_lookup([context.rom for context in contexts], executor=executor, max_pending=3))

    assert results.keys() == set(range(5))
    assert results[0].fields == {"players": 2, "year": 2000}
    assert results[0].identifiers == {}
    assert all(results[index].fields == {"year": 1990, "players": 2} for index in range(1, 5))
    assert all(results[index].field_sources == {"year": "first", "players": "last"} for index in range(1, 5))


@pytest.mark.parametrize("limit", [0, 33, True])
def test_invalid_pending_limit_is_rejected_before_scheduling(
    tmp_path: Path, contexts: list[LookupContext], limit: int | bool,
) -> None:
    class Source(MetadataSource):
        provides = frozenset({"year"})

        def fetch(self, context: LookupContext) -> Metadata:
            raise AssertionError

    source = Source(HttpClient(tmp_path), DatasetCache(tmp_path / "db"))
    executor = ImmediateExecutor()

    with pytest.raises(ValueError, match="Pending metadata batches must be 1..32"):
        list(source.iter_lookup(contexts, executor=executor, max_pending=limit))

    assert executor.submitted == 0


def test_each_provider_reports_progress_before_last_provider_yields(
    rom: Rom, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="icat")
    http = HttpClient(tmp_path / "cache")
    datasets = DatasetCache(tmp_path / "datasets")

    class First(MetadataSource):
        name = "first"
        provides = frozenset({"year"})

        def fetch(self, context: LookupContext) -> Metadata:
            return Metadata(fields={"year": 1991})

    class Last(MetadataSource):
        name = "last"
        provides = frozenset({"players"})

        def fetch(self, context: LookupContext) -> Metadata:
            events = [
                record.icat_provider_progress for record in caplog.records if hasattr(record, "icat_provider_progress")
            ]
            assert any(
                event.source == "first" and event.completed == 1 and event.status == "done" for event in events
            )
            return Metadata(fields={"players": 2})

    provider = Provider([First(http, datasets), Last(http, datasets)])
    before = http.diagnostics.get_snapshot()["events"]

    result = provider.lookup(rom)

    assert result.fields == {"year": 1991, "players": 2}
    assert http.diagnostics.get_snapshot()["events"] == before
    events = [record.icat_provider_progress for record in caplog.records if hasattr(record, "icat_provider_progress")]
    assert events[0].source == "first" and events[0].completed == 0
    assert events[-1].source == "last" and events[-1].status == "done"
