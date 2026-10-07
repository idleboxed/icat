"""Approved aliases, unknown diagnostics and existing-catalogue normalization."""

import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from icat.databases.cache import DatasetCache
from icat.metadata.genres import GENRE_ALIASES, GENRES, normalize_genre
from icat.metadata.types import Metadata, LookupContext
from icat.metadata.provider import Provider
from icat.io.http import HttpClient
from icat.roms.types import Rom
from icat.metadata.sources.base import MetadataSource
from icat.operations.options import Options
from icat.operations.session import run


@pytest.mark.parametrize(("name", "expected"), [
    (name, canonical) for canonical, aliases in GENRE_ALIASES.items() for name in (canonical, *aliases)
])
def test_known_names_share_one_canonical_genre(name: str, expected: str) -> None:
    assert normalize_genre(name) == expected
    assert normalize_genre(expected) == expected


@pytest.mark.parametrize(("name", "expected"), [
    ("  ROLE–PLAYING  ", "RPG"), ("  action   adventure ", "Action Adventure"),
    ("role_playing (rpg)", "RPG"), ("TRIVIA/ GAME SHOW", "Quiz"), ("visual-novel", "Visual Novel"),
])
def test_spelling_variants_are_normalized_without_fuzzy_search(name: str, expected: str) -> None:
    assert normalize_genre(name) == expected


@pytest.mark.parametrize("name", [None, "Unknown", "Indie", "Compilation", "Sci-Fi", "Sim", "Stratejy", [], 7])
def test_unknowns_are_not_recognized_as_known_hierarchy_categories(name: object) -> None:
    assert normalize_genre(name) is None


def test_distinct_gameplay_genres_are_not_merged() -> None:
    assert {"Action", "Action Adventure", "Arcade", "Beat 'em up", "Fighting", "Flight"} <= GENRES
    assert len(GENRES) == 23


@pytest.mark.parametrize(("offered", "expected"), [
    (" role-playing ", "RPG"), ("Sci-Fi", None), ("Indie", None), ("Compilation", None),
    ("Horror", None), ("First-Person", None), ("2D", None), ("Other", "Other"), (None, None),
])
def test_remote_genres_are_normalized_or_warned(
    tmp_path: Path, rom: Rom, caplog: pytest.LogCaptureFixture, offered: str | None, expected: str | None,
) -> None:
    class Source(MetadataSource):
        name = "fixture"
        provides = frozenset({"genre"})

        def fetch(self, context: LookupContext) -> Metadata:
            return Metadata(fields={"genre": offered})

    http = HttpClient(tmp_path)
    provider = Provider([Source(http, DatasetCache(tmp_path / "datasets"))])

    result = provider.lookup(rom)

    assert result.fields.get("genre") == expected

    if offered is not None and expected is None:
        assert f"unknown genre {offered!r}" in caplog.text
        assert "using null" in caplog.text
        assert provider.diagnostics.get_snapshot()["counts"]["fixture"]["issue.unknown_genre"] == 1

    else:
        assert "unknown genre" not in caplog.text


def test_sync_normalizes_existing_catalogue_even_with_empty_source_and_replaces_unknowns(
    options: Options, provider: Any, nes: bytes, caplog: pytest.LogCaptureFixture,
) -> None:
    (options.src / "a.nes").write_bytes(nes)
    (options.src / "b.nes").write_bytes(nes[:-1] + b"D")
    run(replace(options, move_roms=True), provider)
    path = options.dst / "catalogue.json"
    document = json.loads(path.read_text())
    document["games"][0]["genre"] = " Role-Playing "
    document["games"][1]["genre"] = "Sci-Fi"
    path.write_text(json.dumps(document))
    originals = {path: path.read_bytes() for path in (options.dst / "images").rglob("*.nes")}

    class Updated:
        def lookup(self, rom: Rom) -> Metadata:
            return Metadata({"year": 1991}, field_sources={"year": "fixture"})

    with caplog.at_level(logging.INFO, logger="icat"):
        result = run(options, Updated())

    games = {entry["hash"]: entry for entry in json.loads(path.read_text())["games"]}
    normalized, unknown = document["games"]
    assert games[normalized["hash"]]["genre"] == "RPG"
    assert games[unknown["hash"]]["genre"] is None
    assert "unknown genre 'Sci-Fi'" in caplog.text
    assert result.imported == result.removed == 0
    assert all(path.read_bytes() == content for path, content in originals.items())
    journal = json.loads(sorted(options.logs.rglob("journal.json"))[-1].read_text())
    assert journal["metadata_fields"][normalized["hash"]] == {"genre": "icat", "year": "fixture"}
    assert journal["metadata_fields"][unknown["hash"]] == {"genre": "icat", "year": "fixture"}
    assert journal["summary"]["catalogue_changed"] is True
    before = path.read_bytes()
    caplog.clear()

    with caplog.at_level(logging.INFO, logger="icat"):
        run(options, Updated())

    journal = json.loads(sorted(options.logs.rglob("journal.json"))[-1].read_text())
    assert path.read_bytes() == before
    assert journal["metadata_fields"] == {}
    assert journal["summary"]["catalogue_changed"] is False
    assert "catalogue.json — unchanged" in caplog.text
    assert "unknown genre" not in caplog.text


@pytest.mark.parametrize("offered", [[], 7, "", " \t ", "Action\nAdventure"])
def test_invalid_remote_genres_are_not_hidden_as_other(
    tmp_path: Path, rom: Rom, caplog: pytest.LogCaptureFixture, offered: object,
) -> None:
    class Source(MetadataSource):
        name = "fixture"
        provides = frozenset({"genre"})

        def fetch(self, context: LookupContext) -> Metadata:
            return Metadata(fields={"genre": offered})

    http = HttpClient(tmp_path)
    provider = Provider([Source(http, DatasetCache(tmp_path / "datasets"))])

    result = provider.lookup(rom)

    assert "genre" not in result.fields
    assert "invalid genre" in caplog.text
    assert "unknown genre" not in caplog.text


@pytest.mark.parametrize(("offered", "expected"), [
    ("Sport", "Sports"), ("Unknown", None), ("Horror", None), (None, None),
])
def test_sync_normalizes_new_metadata_from_custom_provider(
    options: Options, nes: bytes, offered: str | None, expected: str | None,
) -> None:
    (options.src / "a.nes").write_bytes(nes)

    class Source:
        def lookup(self, rom: Rom) -> Metadata:
            return Metadata({"genre": offered}, field_sources={"genre": "fixture"})

    run(options, Source())

    document = json.loads((options.dst / "catalogue.json").read_text())
    assert document["games"][0]["genre"] == expected


@pytest.mark.parametrize("existing", [None, "Other", "Miscellaneous"])
@pytest.mark.parametrize("first", ["Other", "Indie", "Horror", None])
def test_other_never_blocks_concrete_genre_from_later_source(
    tmp_path: Path, rom: Rom, existing: str | None, first: str | None,
) -> None:
    class Initial(MetadataSource):
        name = "initial"
        provides = frozenset({"genre"})

        def fetch(self, context: LookupContext) -> Metadata:
            return Metadata(fields={"genre": first})

    class Concrete(Initial):
        name = "concrete"

        def fetch(self, context: LookupContext) -> Metadata:
            return Metadata(fields={"genre": "Sport"})

    rom.entry["genre"] = existing
    http = HttpClient(tmp_path)
    cache = DatasetCache(tmp_path / "datasets")
    provider = Provider([Initial(http, cache), Concrete(http, cache)])

    result = provider.lookup(rom)

    assert result.fields == {"genre": "Sports"}
    assert result.field_sources == {"genre": "concrete"}
    assert rom.entry["genre"] == existing


@pytest.mark.parametrize(("existing", "offered", "expected"), [
    ("Other", "Sport", "Sports"), ("Miscellaneous", "Sport", "Sports"),
    ("Sci-Fi", "Strategy", "Strategy"), ("Other", None, "Other"),
    ("Other", "Indie", "Other"), ("Sports", "Other", "Sports"),
    ("Sports", "Racing", "Sports"),
    ("Horror", None, None), ("Indie", None, None), ("Compilation", None, None), ("Sci-Fi", None, None),
    ("Other", "Horror", "Other"), ("Horror", "Board", "Board Games"),
])
def test_sync_refines_any_existing_other_without_overwriting_concrete_genres(
    options: Options, provider: Any, nes: bytes, existing: str, offered: str | None, expected: str | None,
) -> None:
    (options.src / "game.nes").write_bytes(nes)
    run(replace(options, move_roms=True), provider)
    path = options.dst / "catalogue.json"
    document = json.loads(path.read_text())
    document["games"][0]["genre"] = existing
    path.write_text(json.dumps(document))

    class Updated:
        def lookup(self, rom: Rom) -> Metadata:
            return Metadata(fields={"genre": offered}, field_sources={"genre": "fixture"})

    run(options, Updated())

    assert json.loads(path.read_text())["games"][0]["genre"] == expected
