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
from icat.roms.inspection import inspect_rom
from icat.databases.libretro import BASE
from icat.metadata.sources.platforms import LIBRETRO, LIBRETRO_DATASETS


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
        "crc": (crc, "12345678"), "platform": (LIBRETRO_DATASETS["no-intro"]["NES"], LIBRETRO_DATASETS["no-intro"]["MD"]),
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
    system = LIBRETRO_DATASETS["maxusers"]["NES"]
    users = f"clrmamepro ( name \"{system}\" ) game ( users 2 rom ( crc {crc} ) )"
    url = get_thumbnail_url("NES", "Game (Japan)")
    rules = [
        canonical_dat_rule, f"GET {BASE}/maxusers/{quote(system)}.dat -> 200 :{users}",
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


@pytest.mark.parametrize("platform", ["FDS", "SG1000", "SMS", "32X", "NDS"])
def test_additional_platform_uses_its_own_dat_and_thumbnail_repository(
    tmp_path: Path, rom: Rom, canonical_dat_rule: str, png: bytes, response_mock: Callable[..., Any],
    platform: str, thumbnail_index_rules: Callable[..., list[str]],
) -> None:
    rom.entry.update(platform=platform, image=f"{platform}/aa/GoodTools (J) [!].rom")
    systems = LIBRETRO_DATASETS["no-intro"]
    rule = canonical_dat_rule.replace(quote(systems["NES"]), quote(systems[platform]))
    rule = rule.replace(systems["NES"], systems[platform])
    url = get_thumbnail_url(platform, "Game (Japan)")
    pipeline = build_provider(HttpClient(tmp_path), environ={}, source_names=["libretro"])

    with response_mock([rule, *thumbnail_index_rules(platform), f"GET {url} -> 200 :".encode() + png]) as mock:
        result = pipeline.lookup(rom)
        calls = list(mock.calls)

    assert result.thumbnail is not None
    assert len(calls) == 4


@pytest.mark.parametrize("source_names", [["libretro-players", "libretro"], ["libretro", "libretro-players"]])
@pytest.mark.parametrize("platform, failure", [("NDS", "unsupported"), ("SNES", "unsupported"), ("SNES", "404")])
def test_missing_players_dataset_does_not_block_snapshots(
    tmp_path: Path, rom: Rom, canonical_dat_rule: str, png: bytes, response_mock: Callable[..., Any],
    thumbnail_index_rules: Callable[..., list[str]], monkeypatch: pytest.MonkeyPatch,
    source_names: list[str], platform: str, failure: str,
) -> None:
    systems = LIBRETRO_DATASETS["no-intro"]
    system = systems[platform]
    rule = canonical_dat_rule.replace(quote(systems["NES"]), quote(system)).replace(systems["NES"], system)
    rom.entry.update(platform=platform, image=f"{platform}/aa/GoodTools (J) [!].rom")
    url = get_thumbnail_url(platform, "Game (Japan)")
    rules = [rule, *thumbnail_index_rules(platform), f"GET {url} -> 200 :".encode() + png]

    if failure == "unsupported":
        monkeypatch.delitem(LIBRETRO_DATASETS["maxusers"], platform, raising=False)

    else:
        rules.append(f"GET {BASE}/maxusers/{quote(system)}.dat -> 404 :")

    pipeline = build_provider(HttpClient(tmp_path), environ={}, source_names=source_names)

    with response_mock(rules) as mock:
        result = pipeline.lookup(rom)
        calls = list(mock.calls)

    assert result.thumbnail is not None
    assert result.fields == {}
    assert result.sources == [rule.split()[1], url]
    assert sum("/maxusers/" in call.request.url for call in calls) == int(failure == "404")
    assert sum(call.request.url == rule.split()[1] for call in calls) == 1


@pytest.mark.parametrize("mismatch", [None, "sha1", "size", "crc", "platform", "conflict"])
def test_normalized_identity_controls_snapshot_selection(
    tmp_path: Path, lookup_image: Callable[[str], tuple[str, str, bytes, bytes]],
    libretro_dat_rule: Callable[..., str], thumbnail_index_rules: Callable[..., list[str]],
    png: bytes, response_mock: Callable[..., Any], mismatch: str | None,
) -> None:
    platform, suffix, _canonical, encoded = lookup_image("snes")
    path = tmp_path / f"GoodTools (U) [!]{suffix}"
    path.write_bytes(encoded)
    rom = inspect_rom(path, path.name)
    sha1, size = rom.lookups[-1]
    crc = rom.crc32s[rom.lookups[-1]]
    rule = libretro_dat_rule(rom)
    replacements = {
        "sha1": (sha1, "0" * 40), "size": (f"size {size}", "size 1"), "crc": (crc, "12345678"),
        "platform": (LIBRETRO_DATASETS["no-intro"][platform], LIBRETRO_DATASETS["no-intro"]["MD"]),
    }

    if mismatch in replacements:
        rule = rule.replace(*replacements[mismatch])

    elif mismatch == "conflict":
        full_sha1, full_size = rom.full_lookup
        rule += (
            f"game ( name \"Another release\" rom ( sha1 {full_sha1} size {full_size} "
            f"crc {rom.crc32s[rom.full_lookup]} ) )"
        )

    rules = [rule]
    url = get_thumbnail_url(platform, "Canonical (USA)")

    if mismatch not in {"crc", "conflict"}:
        rules.extend(thumbnail_index_rules(platform, names=["Canonical (USA).png"]))

    if mismatch is None:
        rules.append(f"GET {url} -> 200 :".encode() + png)

    pipeline = build_provider(HttpClient(tmp_path / "cache"), environ={}, source_names=["libretro"])

    with response_mock(rules) as mock:
        result = pipeline.lookup(rom)
        calls = list(mock.calls)

    assert (result.thumbnail is not None) == (mismatch is None)
    assert sum(call.request.url == url for call in calls) == int(mismatch is None)

    if mismatch == "conflict":
        assert pipeline.diagnostics.get_snapshot()["counts"]["libretro"]["issue.ambiguous_canonical_name"] == 1


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
