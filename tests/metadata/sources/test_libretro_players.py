import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from icat.metadata.provider import Provider, build_provider
from icat.io.http import HttpClient
from icat.roms.types import Rom
from icat.metadata.types import Metadata
from icat.databases.libretro import BASE
from icat.metadata.sources.libretro_dat import SYSTEMS
from icat.operations.options import Options
from icat.operations.session import run


@pytest.fixture
def dats(rom: Rom) -> dict[str, str]:
    """Minimal synthetic tables; names deliberately do not match the ROM filename."""
    sha1, size = rom.lookups[0]
    crc = rom.crc32s[rom.lookups[0]]
    header = f"clrmamepro ( name \"{SYSTEMS["NES"]}\" )\n"
    return {
        "no-intro": (
            f"{header}game ( name \"Different name\" rom ( sha1 {sha1.upper()} "
            f"size {size} crc {crc.upper()} ) )\n"
        ),
        "maxusers": f"{header}game ( comment \"Another name\" users 2 rom ( crc {crc.upper()} ) )\n",
    }


def build_response_rules(dats: dict[str, str]) -> list[str]:
    return [f"GET {BASE}/{kind}/{quote(SYSTEMS["NES"])}.dat -> 200 :{body}" for kind, body in dats.items()]


def create_provider(path: Path, *, offline: bool = False, refresh: bool = False) -> Provider:
    return build_provider(
        HttpClient(path, offline=offline), environ={}, refresh_databases=refresh, source_names=["libretro-players"]
    )


def test_exact_identity_fills_only_players_and_keeps_both_sources(
    tmp_path: Path, rom: Rom, dats: dict[str, str], response_mock: Callable[..., Any],
) -> None:
    pipeline = create_provider(tmp_path / "cache")

    with response_mock(build_response_rules(dats)):
        result = pipeline.lookup(rom)

    assert result.fields == {"players": 2}
    assert result.field_sources == {"players": "libretro-players"}
    assert result.sources == [f"{BASE}/{kind}/{quote(SYSTEMS["NES"])}.dat" for kind in dats]


@pytest.mark.parametrize("mismatch", ["sha1", "size", "crc", "platform", "crc-only"])
def test_name_or_crc_alone_cannot_identify_a_rom(
    tmp_path: Path, rom: Rom, dats: dict[str, str], response_mock: Callable[..., Any], mismatch: str,
) -> None:
    sha1, size = rom.lookups[0]
    crc = rom.crc32s[rom.lookups[0]]
    old, new = {
        "sha1": (sha1.upper(), "0" * 40),
        "size": (f"size {size}", "size 1"),
        "crc": (crc.upper(), "12345678"),
        "platform": (SYSTEMS["NES"], SYSTEMS["MD"]),
        "crc-only": (f"sha1 {sha1.upper()} size {size}", ""),
    }[mismatch]
    dats["no-intro"] = dats["no-intro"].replace(old, new)
    expected = build_response_rules(dats)[:1] if mismatch in {"platform", "crc-only"} else build_response_rules(dats)

    with response_mock(expected):
        result = create_provider(tmp_path).lookup(rom)

    assert result.fields == {}


@pytest.mark.parametrize("conflict", ["crc-collision", "identity-crcs", "counts", "header-payload", "unknown-count"])
def test_ambiguous_identities_or_counts_are_not_guessed(
    tmp_path: Path, rom: Rom, dats: dict[str, str], response_mock: Callable[..., Any], conflict: str,
) -> None:
    sha1, size = rom.lookups[0]
    crc = rom.crc32s[rom.lookups[0]]

    if conflict == "crc-collision":
        dats["no-intro"] += f"game ( rom ( sha1 {"0" * 40} size 1 crc {crc} ) )"

    elif conflict == "identity-crcs":
        dats["no-intro"] += f"game ( rom ( sha1 {sha1} size {size} crc 12345678 ) )"

    elif conflict == "header-payload":
        sha1, size = rom.lookups[1]
        crc = rom.crc32s[rom.lookups[1]]
        dats["no-intro"] += f"game ( rom ( sha1 {sha1} size {size} crc {crc} ) )"
        dats["maxusers"] += f"game ( users 4 rom ( crc {crc} ) )"

    else:
        count = "0" if conflict == "unknown-count" else "4"
        dats["maxusers"] += f"game ( users {count} rom ( crc {crc} ) )"

    with response_mock(build_response_rules(dats)):
        result = create_provider(tmp_path).lookup(rom)

    assert result.fields == {}


@pytest.mark.parametrize("count", ["0", "-1", "256", "2-4", "two", "\"\""])
def test_invalid_player_count_is_not_turned_into_a_default(
    tmp_path: Path, rom: Rom, dats: dict[str, str], response_mock: Callable[..., Any], count: str,
) -> None:
    dats["maxusers"] = dats["maxusers"].replace("users 2", f"users {count}")

    with response_mock(build_response_rules(dats)):
        assert create_provider(tmp_path).lookup(rom).fields == {}


def test_duplicate_identical_records_are_harmless(
    tmp_path: Path, rom: Rom, dats: dict[str, str], response_mock: Callable[..., Any],
) -> None:

    for kind, body in list(dats.items()):
        dats[kind] += body.split("\n", 1)[1]

    with response_mock(build_response_rules(dats)):
        assert create_provider(tmp_path).lookup(rom).fields == {"players": 2}


def test_filled_players_and_unsupported_platform_require_no_requests(
    tmp_path: Path, rom: Rom, response_mock: Callable[..., Any],
) -> None:
    pipeline = create_provider(tmp_path / "not-created")
    rom.entry["players"] = 7

    with response_mock([]) as mock:
        assert pipeline.lookup(rom).fields == {}
        rom.entry.update(players=None, platform="NDS")
        assert pipeline.lookup(rom).fields == {}
        assert not mock.calls

    assert not (tmp_path / "not-created").exists()


def test_dat_pair_is_fetched_once_per_run_and_reused_online_or_offline(
    tmp_path: Path, rom: Rom, dats: dict[str, str], response_mock: Callable[..., Any],
) -> None:
    cache = tmp_path / "cache"
    pipeline = create_provider(cache)

    def lookup(_number: int) -> Metadata:
        return pipeline.lookup(rom)

    with response_mock(build_response_rules(dats)) as mock:

        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lookup, range(10)))

        assert len(mock.calls) == 2

    assert all(result.fields == {"players": 2} for result in results)

    for offline in (False, True):

        with response_mock([]) as mock:
            assert create_provider(cache, offline=offline).lookup(rom).fields == {"players": 2}
            assert not mock.calls


@pytest.mark.parametrize("bad_update", ["wrong-platform", "truncated", "wrong-kind", "empty", "unavailable"])
def test_failed_refresh_preserves_validated_dat_pair(
    tmp_path: Path, rom: Rom, dats: dict[str, str], response_mock: Callable[..., Any], bad_update: str,
) -> None:
    cache = tmp_path / "cache"

    with response_mock(build_response_rules(dats)):
        create_provider(cache).lookup(rom)

    manifests = {path: path.read_bytes() for path in cache.glob("datasets/*/current.json")}
    updated = dict(dats)
    updated["maxusers"] = {
        "wrong-platform": dats["maxusers"].replace(SYSTEMS["NES"], SYSTEMS["MD"]),
        "truncated": dats["maxusers"][:-3],
        "wrong-kind": dats["no-intro"],
        "empty": dats["maxusers"].split("\n")[0],
        "unavailable": "",
    }[bad_update]
    responses = build_response_rules(updated)

    if bad_update == "unavailable":
        responses[1] = responses[1].replace("200 :", "500 :")

    with response_mock(responses):
        result = create_provider(cache, refresh=True).lookup(rom)

    assert result.fields == {"players": 2}
    assert all(path.read_bytes() == original for path, original in manifests.items())


def test_offline_missing_or_corrupt_dataset_does_not_use_network(
    tmp_path: Path, rom: Rom, dats: dict[str, str], response_mock: Callable[..., Any],
) -> None:
    cache = tmp_path / "cache"

    with response_mock([]):
        assert create_provider(cache, offline=True).lookup(rom).fields == {}

    with response_mock(build_response_rules(dats)):
        create_provider(cache).lookup(rom)

    manifest = next(cache.glob("datasets/*maxusers/current.json"))
    digest = json.loads(manifest.read_text())["sha256"]
    (manifest.parent / digest).write_bytes(b"broken")

    with response_mock([]) as mock:
        assert create_provider(cache, offline=True).lookup(rom).fields == {}
        assert not mock.calls


def test_sync_populates_players_and_offline_rerun_preserves_manual_value(
    options: Options, provider: Any, nes: bytes, dats: dict[str, str], response_mock: Callable[..., Any],
) -> None:
    (options.src / "Game.nes").write_bytes(nes)
    run(options, provider)
    catalogue = options.dst / "catalogue.json"
    before = json.loads(catalogue.read_text())
    pipeline = build_provider(HttpClient(options.cache), environ={}, source_names=["libretro-players"])

    with response_mock(build_response_rules(dats)):
        run(options, pipeline)

    after = json.loads(catalogue.read_text())

    assert after["games"][0] == {**before["games"][0], "players": 2}
    after["games"][0]["players"] = 7
    catalogue.write_text(json.dumps(after))

    with response_mock([]) as mock:
        run(options, pipeline)
        assert not mock.calls

    assert json.loads(catalogue.read_text())["games"][0]["players"] == 7


@pytest.fixture
def nes_pair(dats: dict[str, str], rom: Rom) -> dict[str, str]:
    sha1, size = rom.lookups[0]
    crc = rom.crc32s[rom.lookups[0]]
    header = f"{dats["no-intro"].split("\n", 1)[0]}\n"
    dats["no-intro"] = (
        f"{header}"
        f"game ( name \"Canonical (Japan)\" rom ( name \"Canonical (Japan).unh\" "
        f"sha1 {sha1} size {size} crc {crc} ) )\n"
        f"game ( name \"Canonical (Japan)\" rom ( name \"Canonical (Japan).nes\" "
        f"sha1 {"a" * 40} size {size + 16} crc 12345678 ) )\n"
    )
    dats["maxusers"] = f"{header}game ( users 2 rom ( crc 12345678 ) )\n"
    return dats


def test_exact_headerless_identity_links_to_its_unique_canonical_headered_record(
    tmp_path: Path, rom: Rom, nes_pair: dict[str, str], response_mock: Callable[..., Any],
) -> None:

    with response_mock(build_response_rules(nes_pair)):
        result = create_provider(tmp_path).lookup(rom)

    assert result.fields == {"players": 2}


@pytest.mark.parametrize("conflict", ["name", "filename", "size", "duplicate-header", "crc-collision", "counts"])
def test_headerless_bridge_does_not_guess_between_variants(
    tmp_path: Path, rom: Rom, nes_pair: dict[str, str], response_mock: Callable[..., Any], conflict: str,
) -> None:

    if conflict == "name":
        nes_pair["no-intro"] = nes_pair["no-intro"].replace(
            "game ( name \"Canonical (Japan)\" rom ( name \"Canonical (Japan).nes\"",
            "game ( name \"Another release\" rom ( name \"Canonical (Japan).nes\"",
        )

    elif conflict == "filename":
        nes_pair["no-intro"] = nes_pair["no-intro"].replace("Canonical (Japan).nes", "Canonical (USA).nes")

    elif conflict == "size":
        nes_pair["no-intro"] = nes_pair["no-intro"].replace(f"size {rom.lookups[0][1] + 16}", "size 100")

    elif conflict == "duplicate-header":
        line = nes_pair["no-intro"].splitlines()[-1]
        nes_pair["no-intro"] += line.replace("a" * 40, "b" * 40).replace("12345678", "22345678")

    elif conflict == "crc-collision":
        nes_pair["no-intro"] += f"game ( rom ( sha1 {"c" * 40} size 100 crc 12345678 ) )"

    else:
        crc = rom.crc32s[rom.lookups[0]]
        nes_pair["maxusers"] += f"game ( users 4 rom ( crc {crc} ) )"

    with response_mock(build_response_rules(nes_pair)):
        result = create_provider(tmp_path).lookup(rom)

    assert result.fields == {}
