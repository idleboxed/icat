


from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from icat.databases.cache import DatasetCache
from icat.metadata.provider import Provider, build_provider
from icat.io.http import HttpClient, NetworkError
from icat.roms.types import Rom
from icat.metadata.types import LookupContext, Metadata
from icat.metadata.sources.base import MetadataSource


def test_new_subclasses_fill_only_missing_valid_fields_and_pass_ids(
    tmp_path: Path, rom: Rom, response_mock: Callable[..., Any],
) -> None:
    class First(MetadataSource):
        name = "first"
        provides = frozenset({"description", "players", "region"})
        hosts = frozenset({"example.test"})

        def fetch(self, context: LookupContext) -> Metadata:
            return Metadata(self.get_json("https://example.test/one"), identifiers={"test:game": "123"})

    class Second(First):
        name = "second"

        def fetch(self, context: LookupContext) -> Metadata:
            assert context.identifiers["test:game"] == "123"
            assert context.missing & self.provides == {"players"}
            return Metadata(self.get_json("https://example.test/two"))

    http = HttpClient(tmp_path / "cache")
    cache = DatasetCache(tmp_path / "datasets")
    pipeline = Provider([First(http, cache), Second(http, cache)])
    rules = [
        "GET https://example.test/one -> 200 :{\"description\":\"First description.\",\"players\":-1,\"region\":[]}",
        "GET https://example.test/two -> 200 :"
        "{\"description\":\"Do not overwrite.\",\"players\":2,\"region\":[\"USA\"]}",
    ]

    with response_mock(rules):
        result = pipeline.lookup(rom)

    assert result.fields == {"description": "First description.", "players": 2, "region": []}
    assert result.field_sources == {"description": "first", "players": "second", "region": "first"}

def test_failed_source_does_not_block_the_next_source(
    tmp_path: Path, rom: Rom, response_mock: Callable[..., Any],
) -> None:
    class Remote(MetadataSource):
        name = "remote"
        provides = frozenset({"description"})
        hosts = frozenset({"example.test"})

        def fetch(self, context: LookupContext) -> Metadata:
            return Metadata(self.get_json(f"https://example.test/{self.name}"))

    class Fallback(Remote):
        name = "fallback"

    http = HttpClient(tmp_path / "cache")
    cache = DatasetCache(tmp_path / "datasets")
    rules = [
        "GET https://example.test/remote -> 500 :",
        "GET https://example.test/fallback -> 200 :{\"description\":\"Recovered.\"}",
    ]

    with response_mock(rules):
        result = Provider([Remote(http, cache), Fallback(http, cache)]).lookup(rom)

    assert result.fields == {"description": "Recovered."}

def test_programming_error_in_new_source_is_not_disguised_as_network_failure(tmp_path: Path, rom: Rom) -> None:
    class Broken(MetadataSource):
        name = "broken"
        provides = frozenset({"description"})

        def fetch(self, context: LookupContext) -> Metadata:
            raise TypeError("Bug in source implementation")

    source = Broken(HttpClient(tmp_path / "cache"), DatasetCache(tmp_path / "datasets"))

    with pytest.raises(TypeError, match="Bug in source implementation"):
        Provider([source]).lookup(rom)

def test_sources_skip_filled_fields_and_require_no_io_in_constructors(
    tmp_path: Path, rom: Rom, response_mock: Callable[..., Any],
) -> None:
    http = HttpClient(tmp_path / "not-created")
    pipeline = build_provider(http, environ={}, source_names=["openvgdb", "thegamesdb", "libretro"])
    assert not http.cache.exists()
    rom.entry.update(
        title="Manual title", description="Manual description.", year=1992, players=2, genre="Racing", region=[]
    )
    rom.has_thumbnail = True

    with response_mock([]) as mock:
        assert pipeline.lookup(rom).fields == {}
        assert not mock.calls

    assert not http.cache.exists()

def test_source_credentials_cannot_be_forwarded_to_another_host(
    tmp_path: Path, response_mock: Callable[..., Any],
) -> None:
    class KeyedSource(MetadataSource):
        credential_hosts = frozenset({"api.example.test"})

        def fetch(self, context: LookupContext) -> Metadata:
            return Metadata()

    source = KeyedSource(HttpClient(tmp_path), DatasetCache(tmp_path / "db"), key="synthetic-key")

    with response_mock([]) as mock:

        with pytest.raises(NetworkError, match="Credentials are restricted"):
            source.get_json("https://images.example.test/image", headers={"X-Client-API-Key": source.key})

        assert not mock.calls

def test_abstract_source_cannot_be_instantiated(tmp_path: Path) -> None:

    with pytest.raises(TypeError, match="abstract"):
        MetadataSource(HttpClient(tmp_path), DatasetCache(tmp_path / "db"))

def test_source_selection_is_explicit_and_shared_http_limit_is_kept(tmp_path: Path) -> None:
    http = HttpClient(tmp_path, concurrency=3)
    provider = build_provider(http, environ={}, source_names=["libretro", "hasheous"])

    assert [source.name for source in provider.sources] == ["libretro", "hasheous"]
    assert all(source.http is http for source in provider.sources)

    for names in (["unknown"], ["igdb"], ["hasheous", "hasheous"]):

        with pytest.raises(ValueError, match="Unknown or duplicate"):
            build_provider(http, environ={}, source_names=names)

    assert not list(tmp_path.iterdir())

def test_default_sources_do_not_include_igdb_or_use_its_old_key(tmp_path: Path) -> None:
    provider = build_provider(HttpClient(tmp_path), environ={"ICAT_HASHEOUS_API_KEY": "synthetic-unused-key"})

    assert [source.name for source in provider.sources] == [
        "hasheous", "openvgdb", "libretro-players", "thegamesdb", "libretro",
    ]
    assert all(source.key is None for source in provider.sources)
    assert not list(tmp_path.iterdir())
