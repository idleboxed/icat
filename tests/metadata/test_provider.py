import json
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from icat.databases.cache import DatasetCache
from icat.metadata.types import Metadata, LookupContext
from icat.metadata.provider import Provider, build_provider
from icat.io.http import HttpClient
from icat.roms.types import Rom
from icat.roms.inspection import inspect_rom
from icat.metadata.sources.base import MetadataSource
from icat.metadata.sources.openvgdb import URL


LOOKUP = "https://hasheous.org/api/v1/Lookup/ByHash/sha1/"
SNAPSHOT = (
    "https://raw.githubusercontent.com/libretro-thumbnails/Nintendo_-_Nintendo_Entertainment_System"
    "/master/Named_Snaps/Game%20%28Japan%29.png"
)


@pytest.mark.parametrize("full_match", [True, False])
def test_nes_extra_data_prefers_full_hash_then_uses_payload_fallback(
    tmp_path: Path, nes: bytes, lookup_response: dict, response_mock: Callable[..., Any], full_match: bool,
) -> None:
    path = tmp_path / "Extra.nes"
    path.write_bytes(nes + b"B" * 8192)
    rom = inspect_rom(path, path.name)
    full_url = f"{LOOKUP}{rom.lookups[0][0]}"
    payload_url = f"{LOOKUP}{rom.lookups[1][0]}"

    if full_match:
        lookup_response["name"] = "Full dump match"
        lookup_response["signature"]["rom"].update(sha1=rom.lookups[0][0], size=rom.lookups[0][1])
        rules = [f"GET {full_url} -> 200 :{json.dumps(lookup_response)}"]

    else:
        lookup_response["name"] = "Payload match"
        rules = [f"GET {full_url} -> 404 :", f"GET {payload_url} -> 200 :{json.dumps(lookup_response)}"]

    provider = build_provider(HttpClient(tmp_path / "cache"), environ={}, source_names=["hasheous"])

    with response_mock(rules) as mock:
        result = provider.lookup(rom)
        assert [call.request.url for call in mock.calls] == ([full_url] if full_match else [full_url, payload_url])

    assert result.fields["title"] == ("Full dump match" if full_match else "Payload match")
    assert path.read_bytes() == nes + b"B" * 8192


def test_verified_hash_match_fills_metadata_and_fetches_exact_snapshot(
    tmp_path: Path, rom: Rom, lookup_rule: str, png: bytes, response_mock: Callable[..., Any],
    canonical_dat_rule: str, thumbnail_index_rules: Callable[..., list[str]],
) -> None:
    http = HttpClient(tmp_path / "cache")

    rules = [lookup_rule, canonical_dat_rule, *thumbnail_index_rules(), f"GET {SNAPSHOT} -> 200 :".encode() + png]

    with response_mock(rules) as mock:
        result = build_provider(http, environ={}, source_names=["hasheous", "libretro"]).lookup(rom)

        assert result.fields == {
            "title": "Synthetic Game",
            "year": 1991,
            "description": "Synthetic description.",
            "region": ["Japan"],
        }
        assert result.thumbnail is not None
        assert [call.request.url for call in mock.calls] == [
            f"{LOOKUP}{rom.lookups[0][0]}", canonical_dat_rule.split()[1],
            *[rule.split()[1] for rule in thumbnail_index_rules()], SNAPSHOT,
        ]
        assert all("X-Client-API-Key" not in call.request.headers for call in mock.calls)


@pytest.mark.parametrize("mismatch", ["hash", "size", "platform"])
def test_mismatch_does_not_accept_wrong_metadata(
    tmp_path: Path, rom: Rom, lookup_response: dict, mismatch: str, response_mock: Callable[..., Any],
    canonical_dat_rule: str, thumbnail_index_rules: Callable[..., list[str]],
) -> None:
    response = deepcopy(lookup_response)

    if mismatch == "hash":
        response["signature"]["rom"]["sha1"] = "0" * 40

    elif mismatch == "size":
        response["signature"]["rom"]["size"] = 2

    else:
        response["platform"]["metadata"][0]["id"] = "29"

    urls = [f"{LOOKUP}{digest}" for digest, _size in rom.lookups]
    rules = [f"GET {url} -> 200 :{json.dumps(response)}" for url in urls]
    rules.extend([canonical_dat_rule, *thumbnail_index_rules(), f"GET {SNAPSHOT} -> 404 :"])

    with response_mock(rules) as mock:
        result = build_provider(
            HttpClient(tmp_path / "cache"), environ={}, source_names=["hasheous", "libretro"]
        ).lookup(rom)

        assert result.fields == {}
        assert result.thumbnail is None
        assert [call.request.url for call in mock.calls] == [
            *urls, canonical_dat_rule.split()[1],
            *[rule.split()[1] for rule in thumbnail_index_rules()], SNAPSHOT,
        ]


def test_provider_outage_leaves_unknowns_but_still_attempts_image(
    tmp_path: Path, rom: Rom, png: bytes, response_mock: Callable[..., Any],
    canonical_dat_rule: str, thumbnail_index_rules: Callable[..., list[str]],
) -> None:
    url = f"{LOOKUP}{rom.lookups[0][0]}"

    rules = [
        f"GET {url} -> 500 :", canonical_dat_rule, *thumbnail_index_rules(),
        f"GET {SNAPSHOT} -> 200 :".encode() + png,
    ]

    with response_mock(rules) as mock:
        result = build_provider(
            HttpClient(tmp_path / "cache"), environ={}, source_names=["hasheous", "libretro"]
        ).lookup(rom)

        assert result.fields == {}
        assert result.thumbnail is not None
        assert [call.request.url for call in mock.calls] == [
            url, canonical_dat_rule.split()[1],
            *[rule.split()[1] for rule in thumbnail_index_rules()], SNAPSHOT,
        ]


@pytest.mark.parametrize("signature", [None, [], {"rom": None}])
def test_malformed_external_record_does_not_block_other_sources(
    tmp_path: Path, rom: Rom, png: bytes, response_mock: Callable[..., Any], signature: object,
    canonical_dat_rule: str, thumbnail_index_rules: Callable[..., list[str]],
) -> None:
    url = f"{LOOKUP}{rom.lookups[0][0]}"
    rules = [
        f"GET {url} -> 200 :{json.dumps({"signature": signature})}",
        canonical_dat_rule, *thumbnail_index_rules(), f"GET {SNAPSHOT} -> 200 :".encode() + png,
    ]
    provider = build_provider(HttpClient(tmp_path), environ={}, source_names=["hasheous", "libretro"])

    with response_mock(rules):
        result = provider.lookup(rom)

    assert result.fields == {}
    assert result.thumbnail is not None


def test_hasheous_rom_label_is_not_a_description(
    tmp_path: Path, rom: Rom, lookup_response: dict, response_mock: Callable[..., Any],
) -> None:
    lookup_response["signature"]["game"]["description"] = lookup_response["name"]
    url = f"{LOOKUP}{rom.lookups[0][0]}"
    provider = build_provider(HttpClient(tmp_path), environ={}, source_names=["hasheous"])

    with response_mock(f"GET {url} -> 200 :{json.dumps(lookup_response)}"):
        result = provider.lookup(rom)

    assert "description" not in result.fields
    assert result.fields["title"] == "Synthetic Game"


@pytest.mark.parametrize("title", ["ZZZ", "zzz", " ZZZ "])
def test_hasheous_placeholder_allows_other_sources_without_leaking_card(
    tmp_path: Path, rom: Rom, lookup_response: dict, response_mock: Callable[..., Any],
    openvgdb_archive: Callable[..., bytes], title: str, caplog: pytest.LogCaptureFixture,
) -> None:
    lookup_response["name"] = title
    lookup_response["signature"]["game"]["description"] = "Wrong category description"
    provider = build_provider(HttpClient(tmp_path), environ={}, source_names=["hasheous", "openvgdb"])
    rules = [
        f"GET {LOOKUP}{rom.lookups[0][0]} -> 200 :{json.dumps(lookup_response)}",
        f"GET {URL} -> 200 :".encode() + openvgdb_archive(),
    ]

    with response_mock(rules) as mock:
        result = provider.lookup(rom)
        assert len(mock.calls) == 2

    assert result.fields["title"] == "Synthetic Game"
    assert result.fields["description"] != "Wrong category description"
    assert set(result.field_sources.values()) == {"openvgdb"}
    assert result.sources == [URL]
    assert result.identifiers == {"title": "Synthetic Game"}
    assert provider.diagnostics.get_snapshot()["counts"]["hasheous"]["lookup.ignored_placeholder"] == 1
    assert "ZZZ metadata from hasheous ignored" in caplog.text


def test_ignored_placeholder_does_not_hide_a_related_identity_issue(tmp_path: Path, rom: Rom) -> None:
    class Source(MetadataSource):
        name = "fixture"
        provides = frozenset({"title"})
        identifies = True

        def fetch(self, context: LookupContext) -> Metadata:
            self.http.diagnostics.emit("issue", "identity_mismatch", field="platform")
            return Metadata(fields={"title": "ZZZ"})

    source = Source(HttpClient(tmp_path), DatasetCache(tmp_path / "db"))

    result = Provider([source]).lookup(rom)

    assert result.fields == {}
    counts = source.http.diagnostics.get_snapshot()["counts"]["fixture"]
    assert counts["metadata.ignored_placeholder"] == 1
    assert counts["lookup.unavailable"] == 1


@pytest.mark.parametrize("existing_title", [None, "ZZZ", "My manual title"])
def test_placeholder_card_does_not_mutate_the_input_entry(
    tmp_path: Path, rom: Rom, lookup_response: dict, response_mock: Callable[..., Any], existing_title: str | None,
) -> None:

    if existing_title is not None:
        rom.entry["title"] = existing_title

    before = dict(rom.entry)
    lookup_response["name"] = "ZZZ"
    provider = build_provider(HttpClient(tmp_path), environ={}, source_names=["hasheous"])

    with response_mock(f"GET {LOOKUP}{rom.lookups[0][0]} -> 200 :{json.dumps(lookup_response)}"):
        result = provider.lookup(rom)

    assert rom.entry == before
    assert result.fields == result.identifiers == result.field_sources == {}
    assert result.sources == []


@pytest.mark.parametrize("field", ["sha1", "size", "platform"])
def test_unverified_zzz_does_not_bypass_identity_checks(
    tmp_path: Path, rom: Rom, lookup_response: dict, response_mock: Callable[..., Any], field: str,
) -> None:
    lookup_response["name"] = "ZZZ"

    if field == "platform":
        lookup_response["platform"]["metadata"][0]["id"] = "29"

    else:
        lookup_response["signature"]["rom"][field] = "0" * 40 if field == "sha1" else 17

    rules = [
        f"GET {LOOKUP}{rom.lookups[0][0]} -> 200 :{json.dumps(lookup_response)}",
        f"GET {LOOKUP}{rom.lookups[1][0]} -> 404 :",
    ]
    provider = build_provider(HttpClient(tmp_path), environ={}, source_names=["hasheous"])

    with response_mock(rules):
        result = provider.lookup(rom)

    assert result.fields == {}
    counts = provider.diagnostics.get_snapshot()["counts"]["hasheous"]
    assert counts["issue.identity_mismatch"] >= 1
    assert "metadata.ignored_placeholder" not in counts


@pytest.mark.parametrize("title", ["ZZZ", " zzz "])
def test_placeholder_card_drops_all_fields_ids_and_thumbnail(tmp_path: Path, rom: Rom, png: bytes, title: str) -> None:
    class Source(MetadataSource):
        name = "fixture"
        provides = frozenset({"title", "description", "year", "thumbnail"})
        identifies = True

        def fetch(self, context: LookupContext) -> Metadata:
            return Metadata(
                fields={"title": title, "description": "Wrong category", "year": 1991},
                thumbnail=png, sources=["https://example.test/category"], identifiers={"test:game": "42"},
            )

    source = Source(HttpClient(tmp_path), DatasetCache(tmp_path / "db"))

    result = Provider([source]).lookup(rom)

    assert result.fields == result.identifiers == result.field_sources == {}
    assert result.thumbnail is None
    assert result.sources == []


@pytest.mark.parametrize(("field", "wrong", "expected_observed"), [
    ("sha1", "0" * 40, "0" * 40), ("size", 17, 17), ("platform", "29", ["29"]),
    ("sha1", "not-a-sha1-private-body", None),
])
def test_identity_mismatch_reports_the_specific_field_without_arbitrary_response_text(
    tmp_path: Path, rom: Rom, lookup_response: dict, response_mock: Callable[..., Any],
    caplog: pytest.LogCaptureFixture, field: str, wrong: str | int, expected_observed: object,
) -> None:

    if field == "platform":
        lookup_response["platform"]["metadata"][0]["id"] = wrong

    else:
        lookup_response["signature"]["rom"][field] = wrong

    rules = [
        f"GET {LOOKUP}{rom.lookups[0][0]} -> 200 :{json.dumps(lookup_response)}",
        f"GET {LOOKUP}{rom.lookups[1][0]} -> 404 :",
    ]
    pipeline = build_provider(HttpClient(tmp_path), environ={}, source_names=["hasheous"])

    with response_mock(rules):
        result = pipeline.lookup(rom)

    assert result.fields == {}
    issues = pipeline.diagnostics.get_snapshot()["issues"]
    assert len(issues) == 1
    assert issues[0]["first"]["field"] == field
    assert issues[0]["first"]["observed"] == expected_observed
    assert "expected" in issues[0]["first"]
    assert "not-a-sha1-private-body" not in f"{caplog.text}{json.dumps(issues)}"
