"""Synthetic DAT identities and image responses; no live repositories or ROMs."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from icat.metadata.provider import build_provider
from icat.io.http import HttpClient
from icat.roms.types import Rom
from icat.databases.libretro import BASE
from icat.metadata.sources.libretro_dat import SYSTEMS
from icat.metadata.sources.platforms import LIBRETRO


def get_thumbnail_url(platform: str, name: str) -> str:
    repository = LIBRETRO[platform].replace(" ", "_")
    return f"https://raw.githubusercontent.com/libretro-thumbnails/{repository}/master/Named_Snaps/{quote(name)}.png"


def test_dat_name_overrides_goodtools_filename_even_with_filled_players(
    tmp_path: Path, rom: Rom, canonical_dat_rule: str, png: bytes,
    response_mock: Callable[..., Any], thumbnail_index_rules: Callable[..., list[str]],
) -> None:
    rom.entry.update(image="NES/aa/Game (J) [!].nes", title="My own title", players=2)
    pipeline = build_provider(HttpClient(tmp_path), environ={}, source_names=["libretro"])
    url = get_thumbnail_url("NES", "Game (Japan)")

    with response_mock([canonical_dat_rule, *thumbnail_index_rules(), f"GET {url} -> 200 :".encode() + png]) as mock:
        result = pipeline.lookup(rom)
        calls = list(mock.calls)

    assert result.thumbnail is not None
    assert result.fields == {}
    assert result.sources == [canonical_dat_rule.split()[1], url]
    assert len(calls) == 4
    assert rom.entry["title"] == "My own title"


@pytest.mark.parametrize("mismatch", ["sha1", "size", "crc", "platform"])
def test_unverified_dat_name_is_not_requested(
    tmp_path: Path, rom: Rom, canonical_dat_rule: str, response_mock: Callable[..., Any],
    mismatch: str, thumbnail_index_rules: Callable[..., list[str]],
) -> None:
    sha1, size = rom.lookups[0]
    crc = rom.crc32s[rom.lookups[0]]
    before, after = {
        "sha1": (sha1, "0" * 40), "size": (f"size {size}", "size 1"),
        "crc": (crc, "12345678"), "platform": (SYSTEMS["NES"], SYSTEMS["MD"]),
    }[mismatch]
    rule = canonical_dat_rule.replace("name \"Game (Japan)\"", "name \"Wrong release (USA)\"")
    rule = rule.replace(before, after)
    original = get_thumbnail_url("NES", "Game (Japan)")
    responses = [rule]

    if mismatch != "crc":
        responses.extend([*thumbnail_index_rules(), f"GET {original} -> 404 :"])

    pipeline = build_provider(HttpClient(tmp_path), environ={}, source_names=["libretro"])

    with response_mock(responses) as mock:
        result = pipeline.lookup(rom)
        calls = list(mock.calls)

    assert result.thumbnail is None
    assert all("Wrong%20release" not in call.request.url for call in calls)


def test_conflicting_payload_and_full_file_names_do_not_pick_a_picture(
    tmp_path: Path, rom: Rom, canonical_dat_rule: str, response_mock: Callable[..., Any],
) -> None:
    sha1, size = rom.lookups[1]
    crc = rom.crc32s[rom.lookups[1]]
    rule = f"{canonical_dat_rule}game ( name \"Other (USA)\" rom ( sha1 {sha1} size {size} crc {crc} ) )"
    pipeline = build_provider(HttpClient(tmp_path), environ={}, source_names=["libretro"])

    with response_mock(rule) as mock:
        result = pipeline.lookup(rom)
        calls = list(mock.calls)

    assert result.thumbnail is None
    assert len(calls) == 1
    assert pipeline.diagnostics.get_snapshot()["counts"]["libretro"]["issue.ambiguous_canonical_name"] == 1


def test_dat_download_and_parse_are_shared_with_players(
    tmp_path: Path, rom: Rom, canonical_dat_rule: str, png: bytes,
    response_mock: Callable[..., Any], thumbnail_index_rules: Callable[..., list[str]],
) -> None:
    crc = rom.crc32s[rom.lookups[0]]
    users = f"clrmamepro ( name \"{SYSTEMS["NES"]}\" ) game ( users 2 rom ( crc {crc} ) )"
    url = get_thumbnail_url("NES", "Game (Japan)")
    rules = [
        canonical_dat_rule, f"GET {BASE}/maxusers/{quote(SYSTEMS["NES"])}.dat -> 200 :{users}",
        *thumbnail_index_rules(), f"GET {url} -> 200 :".encode() + png,
    ]
    pipeline = build_provider(HttpClient(tmp_path), environ={}, source_names=["libretro-players", "libretro"])

    with response_mock(rules) as mock:
        result = pipeline.lookup(rom)
        calls = list(mock.calls)

    assert result.fields == {"players": 2}
    assert result.thumbnail is not None
    assert len(calls) == 5
    assert pipeline.sources[0].tables is pipeline.sources[1].tables
    assert len(pipeline.sources[0].tables) == 2


@pytest.mark.parametrize("platform", ["FDS", "SG1000", "SMS", "32X"])
def test_additional_platform_uses_its_own_dat_and_thumbnail_repository(
    tmp_path: Path, rom: Rom, canonical_dat_rule: str, png: bytes, response_mock: Callable[..., Any],
    platform: str, thumbnail_index_rules: Callable[..., list[str]],
) -> None:
    rom.entry["platform"] = platform
    rule = canonical_dat_rule.replace(quote(SYSTEMS["NES"]), quote(SYSTEMS[platform]))
    rule = rule.replace(SYSTEMS["NES"], SYSTEMS[platform])
    url = get_thumbnail_url(platform, "Game (Japan)")
    pipeline = build_provider(HttpClient(tmp_path), environ={}, source_names=["libretro"])

    with response_mock([rule, *thumbnail_index_rules(platform), f"GET {url} -> 200 :".encode() + png]) as mock:
        result = pipeline.lookup(rom)
        calls = list(mock.calls)

    assert result.thumbnail is not None
    assert len(calls) == 4


def test_unsupported_thumbnail_platform_is_explicit_and_does_not_use_network(
    tmp_path: Path, rom: Rom, response_mock: Callable[..., Any],
) -> None:
    rom.entry["platform"] = "PS2"
    pipeline = build_provider(HttpClient(tmp_path), environ={}, source_names=["libretro"])

    with response_mock([]) as mock:
        result = pipeline.lookup(rom)
        calls = list(mock.calls)

    assert result.thumbnail is None
    assert not calls
    assert pipeline.diagnostics.get_snapshot()["counts"]["libretro"]["lookup.unsupported_platform"] == 1


def test_cached_file_index_avoids_requests_for_absent_pictures_across_runs(
    tmp_path: Path, rom: Rom, canonical_dat_rule: str,
    thumbnail_index_rules: Callable[..., list[str]], response_mock: Callable[..., Any],
) -> None:
    pipeline = build_provider(HttpClient(tmp_path), environ={}, source_names=["libretro"])
    rules = [canonical_dat_rule, *thumbnail_index_rules(names=["Another game.png"])]

    with response_mock(rules) as mock:
        assert pipeline.lookup(rom).thumbnail is None
        assert pipeline.lookup(rom).thumbnail is None
        assert len(mock.calls) == 3

    with response_mock([]) as mock:
        again = build_provider(HttpClient(tmp_path), environ={}, source_names=["libretro"])
        assert again.lookup(rom).thumbnail is None
        assert not mock.calls

    assert pipeline.diagnostics.get_snapshot()["counts"]["libretro"]["thumbnail.not_listed"] == 2


@pytest.mark.parametrize("failure", ["http", "truncated", "malformed", "invalid_entry"])
def test_optional_index_failure_does_not_prevent_direct_download(
    tmp_path: Path, rom: Rom, canonical_dat_rule: str, thumbnail_index_rules: Callable[..., list[str]],
    response_mock: Callable[..., Any], png: bytes, failure: str,
) -> None:
    root = thumbnail_index_rules()[0].split()[1]

    if failure == "http":
        index = f"GET {root} -> 403 :"

    else:
        body = {"truncated": failure == "truncated", "tree": {} if failure == "malformed" else []}

        if failure == "invalid_entry":
            body["tree"] = [None]

        index = f"GET {root} -> 200 :{json.dumps(body)}"

    url = get_thumbnail_url("NES", "Game (Japan)")
    pipeline = build_provider(HttpClient(tmp_path), environ={}, source_names=["libretro"])

    with response_mock([canonical_dat_rule, index, f"GET {url} -> 200 :".encode() + png]) as mock:
        result = pipeline.lookup(rom)
        assert len(mock.calls) == 3

    assert result.thumbnail is not None


def test_refresh_updates_cached_thumbnail_names(
    tmp_path: Path, rom: Rom, canonical_dat_rule: str, thumbnail_index_rules: Callable[..., list[str]],
    response_mock: Callable[..., Any], png: bytes,
) -> None:
    pipeline = build_provider(HttpClient(tmp_path), environ={}, source_names=["libretro"])

    with response_mock([canonical_dat_rule, *thumbnail_index_rules(names=[])]):
        assert pipeline.lookup(rom).thumbnail is None

    url = get_thumbnail_url("NES", "Game (Japan)")
    refreshed = build_provider(HttpClient(tmp_path, refresh=True), environ={}, source_names=["libretro"])

    with response_mock([*thumbnail_index_rules(), f"GET {url} -> 200 :".encode() + png]) as mock:
        result = refreshed.lookup(rom)
        assert len(mock.calls) == 3

    assert result.thumbnail is not None
