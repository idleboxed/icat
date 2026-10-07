"""Synthetic VS/PC10 identities; no real ROMs or live Hasheous responses.

SPDX-License-Identifier: BSD-3-Clause
"""

import hashlib
import json
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from icat.metadata.provider import Provider, build_provider
from icat.io.http import HttpClient
from icat.roms.types import Rom
from icat.roms.inspection import inspect_rom
from icat.operations.options import Options
from icat.operations.session import run


LOOKUP = "https://hasheous.org/api/v1/Lookup/ByHash/sha1/"


@pytest.fixture
def variant_response(lookup_response: dict) -> Callable[..., dict]:
    def make(identity: tuple[str, int], *, platform: str = "52", variant: Any = "VS UniSystem") -> dict:
        response = deepcopy(lookup_response)
        response["name"] = "Verified whole file"
        response["signature"]["rom"].update(sha1=identity[0], size=identity[1])
        response["signature"]["game"]["systemVariant"] = variant
        response["platform"]["metadata"][0]["id"] = platform
        return response

    return make


@pytest.fixture
def variant_provider(tmp_path: Path) -> Provider:
    return build_provider(HttpClient(tmp_path / "cache"), environ={}, source_names=["hasheous"])


def build_response_rule(identity: tuple[str, int], response: dict) -> str:
    return f"GET {LOOKUP}{identity[0]} -> 200 :{json.dumps(response)}"


@pytest.mark.parametrize("extra", [b"", b"B" * 8192], ids=["no_tail", "with_tail"])
def test_full_file_identity_is_explicit_and_does_not_change_lookup_order(
    tmp_path: Path, nes: bytes, extra: bytes,
) -> None:
    path = tmp_path / "Game.nes"
    data = nes + extra
    path.write_bytes(data)

    rom = inspect_rom(path, path.name)

    assert rom.full_lookup == (hashlib.sha1(data).hexdigest(), len(data))
    assert rom.lookups[0 if extra else 1] == rom.full_lookup
    assert rom.entry["hash"] == hashlib.sha256(data).hexdigest()
    assert path.read_bytes() == data


@pytest.mark.parametrize("variant", ["VS UniSystem", "PlayChoice-10"])
@pytest.mark.parametrize("platform", ["18", "52"])
@pytest.mark.parametrize("extra", [b"", b"B" * 8192], ids=["no_tail", "with_tail"])
def test_verified_full_variant_accepts_metadata_without_payload_platform_error(
    tmp_path: Path, nes: bytes, variant_response: Callable[..., dict], variant_provider: Provider,
    response_mock: Callable[..., Any], variant: str, platform: str, extra: bytes,
) -> None:
    path = tmp_path / "Game.nes"
    data = nes + extra
    path.write_bytes(data)
    rom = inspect_rom(path, path.name)
    before = dict(rom.entry)
    full = variant_response(rom.full_lookup, platform=platform, variant=variant)
    rules = []

    if not extra:
        payload = variant_response(rom.lookups[0], variant="")
        payload["name"] = "Do not accept payload metadata"
        rules.append(build_response_rule(rom.lookups[0], payload))

    rules.append(build_response_rule(rom.full_lookup, full))

    with response_mock(rules) as mock:
        result = variant_provider.lookup(rom)
        assert len(mock.calls) == len(rules)

    assert result.fields["title"] == "Verified whole file"
    assert result.sources == [LOOKUP + rom.full_lookup[0]]
    assert rom.entry == before
    assert path.read_bytes() == data
    diagnostics = variant_provider.diagnostics.get_snapshot()
    assert diagnostics["issues"] == []
    assert diagnostics["counts"]["hasheous"]["lookup.matched"] == 1
    event = next(entry for entry in diagnostics["events"] if entry["outcome"] == "verified_console_variant")
    assert event["variant"] == variant
    assert event["basis"] == "full_file_sha1_size"
    assert (event["sha1"], event["size"]) == rom.full_lookup
    assert event["platform"] == [platform]
    assert event["recovered_payload_candidates"] == (0 if extra else 1)


@pytest.mark.parametrize("variant", [None, "", "Unknown", "vs unisystem", "VS UniSystem (guess)", {}, []])
def test_arcade_full_hash_without_exact_variant_remains_rejected(
    rom: Rom, variant_response: Callable[..., dict], variant_provider: Provider,
    response_mock: Callable[..., Any], variant: Any,
) -> None:
    response = variant_response(rom.full_lookup, variant=variant)
    rules = [f"GET {LOOKUP}{rom.lookups[0][0]} -> 404 :", build_response_rule(rom.full_lookup, response)]

    with response_mock(rules):
        result = variant_provider.lookup(rom)

    assert result.fields == {}
    assert result.sources == []
    diagnostics = variant_provider.diagnostics.get_snapshot()
    assert diagnostics["issues"][0]["first"]["field"] == "platform"
    assert "identity.verified_console_variant" not in diagnostics["counts"]["hasheous"]


@pytest.mark.parametrize("variant", ["VS UniSystem", "PlayChoice-10"])
def test_payload_only_variant_cannot_authorize_arcade_metadata(
    rom: Rom, variant_response: Callable[..., dict], variant_provider: Provider,
    response_mock: Callable[..., Any], variant: str,
) -> None:
    response = variant_response(rom.lookups[0], variant=variant)
    rules = [build_response_rule(rom.lookups[0], response), f"GET {LOOKUP}{rom.full_lookup[0]} -> 404 :"]

    with response_mock(rules):
        result = variant_provider.lookup(rom)

    assert result.fields == result.field_sources == {}
    diagnostics = variant_provider.diagnostics.get_snapshot()
    assert diagnostics["counts"]["hasheous"]["issue.identity_mismatch"] == 1
    assert diagnostics["counts"]["hasheous"]["lookup.unavailable"] == 1


@pytest.mark.parametrize(
    "field", ["sha1", "size", "platform", "ambiguous_platform", "local_platform", "full_identity"],
)
def test_variant_does_not_bypass_identity_guards(
    rom: Rom, variant_response: Callable[..., dict], variant_provider: Provider,
    response_mock: Callable[..., Any], field: str,
) -> None:
    full_lookup = rom.full_lookup
    response = variant_response(full_lookup)
    response["name"] = "ZZZ"

    if field == "sha1":
        response["signature"]["rom"]["sha1"] = "0" * 40

    elif field == "size":
        response["signature"]["rom"]["size"] += 1

    elif field == "platform":
        response["platform"]["metadata"][0]["id"] = "29"

    elif field == "ambiguous_platform":
        response["platform"]["metadata"].append({**response["platform"]["metadata"][0], "id": "18"})

    elif field == "local_platform":
        rom.entry["platform"] = "FDS"

    else:
        rom.full_lookup = None

    rules = [f"GET {LOOKUP}{rom.lookups[0][0]} -> 404 :", build_response_rule(full_lookup, response)]

    with response_mock(rules):
        result = variant_provider.lookup(rom)

    assert result.fields == {}
    diagnostics = variant_provider.diagnostics.get_snapshot()
    assert diagnostics["issues"]
    assert "identity.verified_console_variant" not in diagnostics["counts"]["hasheous"]


@pytest.mark.parametrize("variant", ["", None, "Unknown"])
def test_nes_full_match_without_variant_does_not_hide_previous_arcade_mismatch(
    rom: Rom, variant_response: Callable[..., dict], variant_provider: Provider,
    response_mock: Callable[..., Any], variant: str | None,
) -> None:
    payload = variant_response(rom.lookups[0], variant="")
    full = variant_response(rom.full_lookup, platform="18", variant=variant)
    rules = [build_response_rule(rom.lookups[0], payload), build_response_rule(rom.full_lookup, full)]

    with response_mock(rules):
        result = variant_provider.lookup(rom)

    assert result.fields["title"] == "Verified whole file"
    diagnostics = variant_provider.diagnostics.get_snapshot()
    assert diagnostics["counts"]["hasheous"]["issue.identity_mismatch"] == 1
    assert diagnostics["counts"]["hasheous"]["lookup.partial"] == 1


@pytest.mark.parametrize("field", ["sha1", "size", "platform"])
def test_verified_variant_preserves_other_failed_identity_checks(
    rom: Rom, variant_response: Callable[..., dict], variant_provider: Provider,
    response_mock: Callable[..., Any], field: str,
) -> None:
    payload = variant_response(rom.lookups[0])

    if field == "platform":
        payload["platform"]["metadata"][0]["id"] = "29"

    else:
        payload["signature"]["rom"][field] = "0" * 40 if field == "sha1" else 1

    full = variant_response(rom.full_lookup)
    rules = [build_response_rule(rom.lookups[0], payload), build_response_rule(rom.full_lookup, full)]

    with response_mock(rules):
        result = variant_provider.lookup(rom)

    assert result.fields["title"] == "Verified whole file"
    diagnostics = variant_provider.diagnostics.get_snapshot()
    assert field in {issue["first"]["field"] for issue in diagnostics["issues"]}
    assert diagnostics["counts"]["hasheous"]["lookup.partial"] == 1


def test_full_lookup_failure_keeps_deferred_payload_mismatch(
    rom: Rom, variant_response: Callable[..., dict], variant_provider: Provider,
    response_mock: Callable[..., Any],
) -> None:
    payload = variant_response(rom.lookups[0])
    rules = [build_response_rule(rom.lookups[0], payload), f"GET {LOOKUP}{rom.full_lookup[0]} -> 500 :"]

    with response_mock(rules):
        result = variant_provider.lookup(rom)

    assert result.fields == {}
    counts = variant_provider.diagnostics.get_snapshot()["counts"]["hasheous"]
    assert counts["issue.identity_mismatch"] == counts["issue.source_error"] == 1


@pytest.mark.parametrize("suffix", ["(VS)", "(PC10)"])
def test_filename_marker_does_not_prove_a_console_variant(
    rom: Rom, variant_response: Callable[..., dict], variant_provider: Provider,
    response_mock: Callable[..., Any], suffix: str,
) -> None:
    rom.entry["image"] = f"Game {suffix}.nes"
    response = variant_response(rom.full_lookup, variant="")
    rules = [f"GET {LOOKUP}{rom.lookups[0][0]} -> 404 :", build_response_rule(rom.full_lookup, response)]

    with response_mock(rules):
        result = variant_provider.lookup(rom)

    assert result.fields == {}
    assert variant_provider.diagnostics.get_snapshot()["counts"]["hasheous"]["lookup.unavailable"] == 1


def test_sync_preserves_rom_and_catalogue_platform_and_records_resolved_variant(
    options: Options, nes: bytes, variant_response: Callable[..., dict], variant_provider: Provider,
    response_mock: Callable[..., Any],
) -> None:
    path = options.src / "Game (VS).nes"
    path.write_bytes(nes)
    rom = inspect_rom(path, path.name)
    payload = variant_response(rom.lookups[0], variant="")
    payload["name"] = "Wrong payload title"
    full = variant_response(rom.full_lookup, platform="18")
    rules = [build_response_rule(rom.lookups[0], payload), build_response_rule(rom.full_lookup, full)]

    with response_mock(rules):
        result = run(options, variant_provider)

    assert result.imported == result.total == 1
    assert result.removed == 0
    entry = json.loads((options.dst / "catalogue.json").read_text())["games"][0]
    assert entry["title"] == "Verified whole file"
    assert entry["platform"] == "NES"
    assert entry["hash"] == hashlib.sha256(nes).hexdigest()
    assert "systemVariant" not in entry
    assert (options.dst / "images" / entry["image"]).read_bytes() == path.read_bytes() == nes
    journal = json.loads(next(options.logs.rglob("journal.json")).read_text())
    assert journal["state"] == journal["metadata_status"] == "complete"
    assert journal["diagnostics"]["issues"] == []
    assert journal["diagnostics"]["counts"]["hasheous"]["identity.verified_console_variant"] == 1
