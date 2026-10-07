import json
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest

from icat.databases.cache import DatasetCache
from icat.metadata.provider import Provider, build_provider
from icat.io.http import HttpClient
from icat.roms.types import Rom
from icat.metadata.types import LookupContext, Metadata
from icat.metadata.sources.base import MetadataSource
from icat.metadata.sources.thegamesdb import TheGamesDbSource


BASE = "https://api.thegamesdb.net/v1"
GENRES = (
    f"GET {BASE}/Genres?apikey=synthetic-secret -> 200 :"
    "{\"data\":{\"genres\":{\"8\":{\"id\":8,\"name\":\"Platformer\"},\"11\":{\"id\":11,\"name\":\"Sports\"}}}}"
)


@pytest.fixture
def tgdb_source(tmp_path: Path) -> TheGamesDbSource:
    return TheGamesDbSource(HttpClient(tmp_path), DatasetCache(tmp_path / "db"), key="synthetic-secret")


@pytest.fixture
def mapped_contexts(rom: Rom) -> Callable[[int], list[LookupContext]]:
    def make(count: int) -> list[LookupContext]:
        contexts = []

        for index in range(count):
            entry = {**rom.entry, "hash": f"{index:064x}", "image": f"Game-{index}.nes", "title": f"Game-{index}"}
            contexts.append(LookupContext(
                replace(rom, entry=entry), dict(entry),
                identifiers={"TheGamesDb:game": f"{index + 1}", "TheGamesDb:platform": "7"},
            ))

        return contexts

    return make


def build_game_rule(ids: str, games: Sequence[dict], **extra: Any) -> str:
    query = urlencode({"apikey": "synthetic-secret", "id": ids, "fields": "overview,players,genres,platform"})
    return f"GET {BASE}/Games/ByGameID?{query} -> 200 :{json.dumps({"data": {"games": games}, **extra})}"


@pytest.fixture
def mapped_lookup(lookup_response: dict) -> str:
    lookup_response["signature"]["game"]["description"] = ""
    lookup_response["metadata"].append({"objectType": "Game", "id": "456", "status": "Mapped", "source": "TheGamesDb"})
    lookup_response["platform"]["metadata"].append(
        {"objectType": "Platform", "id": "7", "status": "Mapped", "source": "TheGamesDb"}
    )
    digest = lookup_response["signature"]["rom"]["sha1"]
    return f"GET https://hasheous.org/api/v1/Lookup/ByHash/sha1/{digest} -> 200 :{json.dumps(lookup_response)}"


@pytest.mark.parametrize("platform", [7, 18])
def test_thegamesdb_uses_hash_mapped_ids_and_checks_platform(
    tmp_path: Path, rom: Rom, mapped_lookup: str, response_mock: Callable[..., Any], platform: int,
) -> None:
    game = {
        "id": 456,
        "platform": platform,
        "game_title": "Synthetic Game",
        "overview": "<p>A real synopsis.</p>",
        "players": 2,
        "genres": [8],
        "release_date": "1992-06-01",
    }
    query = urlencode({"apikey": "synthetic-secret", "id": "456", "fields": "overview,players,genres,platform"})
    rules = [mapped_lookup, f"GET {BASE}/Games/ByGameID?{query} -> 200 :{json.dumps({"data": {"games": [game]}})}"]

    if platform == 7:
        rules.append(
            f"GET {BASE}/Genres?apikey=synthetic-secret -> 200 :"
            "{\"data\":{\"genres\":{\"8\":{\"id\":8,\"name\":\"Platformer\"}}}}"
        )

    pipeline = build_provider(
        HttpClient(tmp_path),
        environ={"ICAT_THEGAMESDB_API_KEY": "synthetic-secret"},
        source_names=["hasheous", "thegamesdb"],
    )

    with response_mock(rules) as mock:
        result = pipeline.lookup(rom)

        assert len(mock.calls) == len(rules)

    if platform == 7:
        assert result.fields["description"] == "A real synopsis."
        assert result.fields["players"] == 2 and result.fields["genre"] == "Platformer"
        assert result.fields["year"] == 1991  # Earlier source wins; filling, not overwriting.

    else:
        assert "description" not in result.fields and "players" not in result.fields

    # This is the hash-verified Hasheous mapping, not proof that TGDB accepted it.
    assert result.identifiers["TheGamesDb:game"] == "456"
    assert result.identifiers["TheGamesDb:platform"] == "7"
    assert "synthetic-secret" not in repr(result)


def test_thegamesdb_without_key_or_mapping_does_not_make_requests(
    tmp_path: Path, rom: Rom, response_mock: Callable[..., Any],
) -> None:

    for environ in ({}, {"ICAT_THEGAMESDB_API_KEY": "synthetic-secret"}):
        pipeline = build_provider(HttpClient(tmp_path), environ=environ, source_names=["thegamesdb"])

        with response_mock([]) as mock:
            assert pipeline.lookup(rom).fields == {}
            assert not mock.calls


@pytest.mark.parametrize("genre_response", ["500 :", "200 :{\"data\":null}", "200 :{\"data\":{\"genres\":[]}}"])
def test_optional_genre_failure_does_not_lose_a_valid_description(
    tmp_path: Path, rom: Rom, mapped_lookup: str, response_mock: Callable[..., Any],
    caplog: pytest.LogCaptureFixture, genre_response: str,
) -> None:
    query = urlencode({"apikey": "synthetic-secret", "id": "456", "fields": "overview,players,genres,platform"})
    game = {"id": 456, "platform": 7, "overview": "Keep this description.", "genres": [8]}
    rules = [
        mapped_lookup,
        f"GET {BASE}/Games/ByGameID?{query} -> 200 :{json.dumps({"data": {"games": [game]}})}",
        f"GET {BASE}/Genres?apikey=synthetic-secret -> {genre_response}",
    ]
    provider = build_provider(
        HttpClient(tmp_path),
        environ={"ICAT_THEGAMESDB_API_KEY": "synthetic-secret"},
        source_names=["hasheous", "thegamesdb"],
    )

    with response_mock(rules):
        result = provider.lookup(rom)

    assert result.fields["description"] == "Keep this description."
    assert "synthetic-secret" not in caplog.text


@pytest.mark.parametrize("first", ["Other", "Miscellaneous", "Indie", "Horror", None])
def test_thegamesdb_selects_concrete_genre_from_one_dictionary(
    tmp_path: Path, rom: Rom, mapped_lookup: str, response_mock: Callable[..., Any], first: str | None,
) -> None:
    query = urlencode({"apikey": "synthetic-secret", "id": "456", "fields": "overview,players,genres,platform"})
    game = {"id": 456, "platform": 7, "genres": [1, 1, 8, 9]}
    dictionary = {"data": {"genres": {"1": {"name": first}, "8": {"name": "Sports"}, "9": {"name": "Racing"}}}}
    rules = [
        mapped_lookup,
        f"GET {BASE}/Games/ByGameID?{query} -> 200 :{json.dumps({"data": {"games": [game]}})}",
        f"GET {BASE}/Genres?apikey=synthetic-secret -> 200 :{json.dumps(dictionary)}",
    ]
    provider = build_provider(
        HttpClient(tmp_path), environ={"ICAT_THEGAMESDB_API_KEY": "synthetic-secret"},
        source_names=["hasheous", "thegamesdb"],
    )

    with response_mock(rules) as mock:
        result = provider.lookup(rom)
        assert len(mock.calls) == 3

    assert result.fields["genre"] == "Sports"


def test_native_batches_split_at_twenty_and_match_reordered_games_by_id(
    tgdb_source: TheGamesDbSource, mapped_contexts: Callable[[int], list[LookupContext]],
    response_mock: Callable[..., Any],
) -> None:
    contexts = mapped_contexts(45)
    rules = []

    for start, end in [(1, 21), (21, 41), (41, 46)]:
        games = [{"id": index, "platform": 7, "players": index, "genres": [11]} for index in range(start, end)]
        rules.append(build_game_rule(",".join(f"{index}" for index in range(start, end)), list(reversed(games))))

    with response_mock([*rules, GENRES]) as mock:
        results = tgdb_source.lookup_many(contexts)

        assert len(mock.calls) == 4  # Three game pages and one dictionary.

    assert [result.fields["players"] for result in results] == list(range(1, 46))
    assert all(result.fields["genre"] == "Sports" for result in results)
    counts = tgdb_source.http.diagnostics.get_snapshot()["counts"]["thegamesdb"]
    assert counts["lookup.matched"] == 45 and counts["batch.lookup"] == 3


def test_duplicate_game_ids_are_requested_once_but_platforms_are_checked_per_rom(
    tgdb_source: TheGamesDbSource, mapped_contexts: Callable[[int], list[LookupContext]],
    response_mock: Callable[..., Any],
) -> None:
    contexts = mapped_contexts(3)

    for context in contexts:
        context.identifiers["TheGamesDb:game"] = "1"

    contexts[-1].identifiers["TheGamesDb:platform"] = "18"

    with response_mock(build_game_rule("1", [{"id": 1, "platform": 7, "players": 2}])) as mock:
        results = tgdb_source.lookup_many(contexts)

        assert len(mock.calls) == 1

    assert [result.fields for result in results] == [{"players": 2}, {"players": 2}, {}]


def test_batch_ignores_missing_unrequested_duplicate_and_wrong_platform_rows(
    tgdb_source: TheGamesDbSource, mapped_contexts: Callable[[int], list[LookupContext]],
    response_mock: Callable[..., Any],
) -> None:
    contexts = mapped_contexts(4)
    games = [
        {"id": 1, "platform": 7, "players": 2},
        {"id": 1, "platform": 7, "players": 3},
        {"id": 2, "platform": 7, "players": 4},
        {"id": 4, "platform": 18, "players": 1},
        {"id": 99, "platform": 7, "players": 8},
    ]

    with response_mock(build_game_rule("1,2,3,4", games)):
        results = tgdb_source.lookup_many(contexts)

    assert [result.fields for result in results] == [{}, {"players": 4}, {}, {}]


@pytest.mark.parametrize("wrong_id", ["", "0", "1,2", "1&apikey=other", True, -1, "9" * 13])
def test_invalid_external_ids_do_not_make_requests(
    tgdb_source: TheGamesDbSource, mapped_contexts: Callable[[int], list[LookupContext]],
    response_mock: Callable[..., Any], wrong_id: Any,
) -> None:
    contexts = mapped_contexts(1)
    contexts[0].identifiers["TheGamesDb:game"] = wrong_id

    with response_mock([]) as mock:
        results = tgdb_source.lookup_many(contexts)

        assert not mock.calls

    assert results[0].outcome == "missing_identifier"


@pytest.mark.parametrize("response", [
    {"data": {"games": {}}},
    {"data": {"games": [None]}},
    {"data": {"games": [{"id": 1, "platform": 7}] * 21}},
    {"data": {"games": [{"id": 1, "platform": 7}]}, "pages": {"next": "https://evil.test/?apikey=synthetic-secret"}},
])
def test_malformed_or_paginated_batch_is_not_followed(
    tgdb_source: TheGamesDbSource, mapped_contexts: Callable[[int], list[LookupContext]],
    response_mock: Callable[..., Any], response: dict,
) -> None:
    url = build_game_rule("1", []).split()[1]

    with response_mock(f"GET {url} -> 200 :{json.dumps(response)}") as mock:
        results = tgdb_source.lookup_many(mapped_contexts(1))

        assert len(mock.calls) == 1

    assert results[0].fields == {} and results[0].outcome == "unavailable"


def test_dictionary_is_shared_by_concurrent_batches(
    tgdb_source: TheGamesDbSource, mapped_contexts: Callable[[int], list[LookupContext]],
    response_mock: Callable[..., Any],
) -> None:
    contexts = mapped_contexts(8)
    rules = [build_game_rule(f"{index}", [{"id": index, "platform": 7, "genres": [11]}]) for index in range(1, 9)]

    with response_mock([*rules, GENRES]) as mock:

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(tgdb_source.lookup, contexts))

        assert len(mock.calls) == 9

    assert all(result.fields["genre"] == "Sports" for result in results)


def test_failed_dictionary_is_not_retried_by_other_batches(
    tgdb_source: TheGamesDbSource, mapped_contexts: Callable[[int], list[LookupContext]],
    response_mock: Callable[..., Any],
) -> None:
    contexts = mapped_contexts(21)
    rules = [
        build_game_rule(
            ",".join(f"{index}" for index in range(1, 21)),
            [{"id": index, "platform": 7, "players": 2, "genres": [11]} for index in range(1, 21)],
        ),
        build_game_rule("21", [{"id": 21, "platform": 7, "players": 2, "genres": [11]}]),
        f"GET {BASE}/Genres?apikey=synthetic-secret -> 500 :",
    ]

    with response_mock(rules) as mock:
        results = tgdb_source.lookup_many(contexts)

        assert len(mock.calls) == 3

    assert all(result.fields == {"players": 2} and result.outcome == "partial" for result in results)


def test_batches_and_dictionary_reuse_sanitized_offline_cache(
    tmp_path: Path, tgdb_source: TheGamesDbSource,
    mapped_contexts: Callable[[int], list[LookupContext]], response_mock: Callable[..., Any],
) -> None:
    contexts = mapped_contexts(2)
    games = [{"id": index, "platform": 7, "genres": [11]} for index in (1, 2)]
    pages = {"current": "https://api.thegamesdb.net/v1/Games/ByGameID?apikey=synthetic-secret", "next": None}

    with response_mock([build_game_rule("1,2", games, pages=pages), GENRES]) as mock:
        online = tgdb_source.lookup_many(contexts)
        assert len(mock.calls) == 2

    offline = TheGamesDbSource(
        HttpClient(tmp_path, offline=True), DatasetCache(tmp_path / "db"), key="synthetic-secret",
    )

    with response_mock([]) as mock:
        results = offline.lookup_many(list(reversed(contexts)))
        assert not mock.calls

    assert [result.fields for result in results] == [result.fields for result in reversed(online)]
    assert all(b"synthetic-secret" not in path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())
    assert "synthetic-secret" not in json.dumps(tgdb_source.http.diagnostics.get_snapshot())


def test_sparse_catalogue_is_packed_after_source_filtering(
    tgdb_source: TheGamesDbSource, mapped_contexts: Callable[[int], list[LookupContext]],
    response_mock: Callable[..., Any],
) -> None:
    contexts = mapped_contexts(2147)
    eligible = {index * 8: index + 1 for index in range(264)}
    filled = set(index for index in range(2147) if index not in eligible)
    filled = set(sorted(filled)[:1181])

    class Identity(MetadataSource):
        name = "identity"
        identifies = True
        provides = TheGamesDbSource.provides

        def fetch(self, context: LookupContext) -> Metadata:
            index = int(context.rom.entry["hash"], 16)

            if index in filled:
                return Metadata(fields={
                    "title": f"Known game {index}", "year": 1990, "players": 1,
                    "genre": "Action", "description": "An existing game description.",
                })

            if index in eligible:
                return Metadata(identifiers={"TheGamesDb:game": f"{eligible[index]}", "TheGamesDb:platform": "7"})

            return Metadata()

    rules = [GENRES]

    for start in range(1, 265, 20):
        ids = list(range(start, min(start + 20, 265)))
        rules.append(build_game_rule(",".join(f"{game_id}" for game_id in ids), [
            {"id": game_id, "platform": 7, "players": 2, "genres": [11]} for game_id in ids
        ]))

    provider = Provider([Identity(tgdb_source.http, tgdb_source.datasets), tgdb_source])

    with response_mock(rules) as mock:
        results = provider.lookup_many([context.rom for context in contexts])

        assert len(mock.calls) == 15

    assert len(results) == 2147
    assert all(results[index].fields["players"] == 2 for index in eligible)
    assert all(results[index].fields["players"] == 1 for index in filled)
    counts = provider.diagnostics.get_snapshot()["counts"]["thegamesdb"]
    assert counts["lookup.already_filled"] == 1181
    assert counts["lookup.missing_identifier"] == 702
    assert counts["lookup.matched"] == 264
    assert counts["batch.lookup"] == 14


def test_duplicate_ids_across_old_chunk_boundaries_share_one_request(
    tgdb_source: TheGamesDbSource, mapped_contexts: Callable[[int], list[LookupContext]],
    response_mock: Callable[..., Any],
) -> None:
    contexts = mapped_contexts(63)

    for index, context in enumerate(contexts):
        context.identifiers["TheGamesDb:game"] = f"{index % 20 + 1}"

    contexts[-1].identifiers["TheGamesDb:platform"] = "18"
    contexts[0].values["players"] = 4
    rules = build_game_rule(",".join(f"{game_id}" for game_id in range(1, 21)), [
        {"id": game_id, "platform": 7, "players": 2} for game_id in range(1, 21)
    ])

    with response_mock(rules) as mock:
        results = tgdb_source.lookup_many(contexts)

        assert len(mock.calls) == 1

    assert results[0].fields == {} and contexts[0].values["players"] == 4
    assert all(result.fields == {"players": 2} for result in results[1:-1])
    assert results[-1].fields == {}


def test_direct_fetch_rejects_more_than_twenty_distinct_ids_before_http(
    tgdb_source: TheGamesDbSource, mapped_contexts: Callable[[int], list[LookupContext]],
    response_mock: Callable[..., Any],
) -> None:

    with response_mock([]) as mock:

        with pytest.raises(ValueError, match="20 distinct game IDs"):
            tgdb_source.fetch_many(mapped_contexts(21))

        assert not mock.calls
