"""Synthetic encoded ROMs retain their bytes while catalogue snapshots are filled.

SPDX-License-Identifier: BSD-3-Clause
"""

import hashlib
import json
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from icat.catalogue.codec import create_empty_entry, decode
from icat.io.http import HttpClient
from icat.io.paths import get_thumbnail_name
from icat.metadata.provider import build_provider
from icat.metadata.sources.platforms import LIBRETRO
from icat.metadata.thumbnails import convert
from icat.operations.options import Options
from icat.operations.session import run
from icat.roms.identity_cache import IdentityCache
from icat.roms.inspection import inspect_rom
from icat.roms.types import Rom


def create_expected_rom(
    path: Path, *, name: str, platform: str, canonical: bytes, encoded: bytes,
) -> Rom:
    full_lookup = hashlib.sha1(encoded).hexdigest(), len(encoded)
    normalized_lookup = hashlib.sha1(canonical).hexdigest(), len(canonical)
    return Rom(
        path, create_empty_entry(name, hashlib.sha256(encoded).hexdigest(), platform),
        [full_lookup, normalized_lookup],
        crc32s={full_lookup: f"{zlib.crc32(encoded):08x}", normalized_lookup: f"{zlib.crc32(canonical):08x}"},
        full_lookup=full_lookup,
    )


def get_snapshot_url(platform: str, title: str) -> str:
    repository = LIBRETRO[platform].replace(" ", "_")
    return (
        f"https://raw.githubusercontent.com/libretro-thumbnails/{repository}/master/Named_Snaps/"
        f"{quote(title, safe="")}.png"
    )


def read_file_state(path: Path) -> tuple[bytes, int, int]:
    info = path.stat()
    return path.read_bytes(), info.st_ino, info.st_mtime_ns


@pytest.mark.parametrize("kind,container", [("snes", "raw"), ("smd", "zip"), ("v64", "7z")])
def test_import_preserves_encoded_rom_and_finds_snapshot(
    options: Options, lookup_image: Callable[[str], tuple[str, str, bytes, bytes]],
    libretro_dat_rule: Callable[..., str], thumbnail_index_rules: Callable[..., list[str]],
    response_mock: Callable[..., Any], sevenzip_archive: Callable[..., Path], png: bytes,
    kind: str, container: str,
) -> None:
    platform, suffix, canonical, encoded = lookup_image(kind)
    name = f"Game (U) [!]{suffix}"
    source = options.src / (name if container == "raw" else f"Game.{container}")

    if container == "zip":

        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr(name, encoded)

    elif container == "7z":
        sevenzip_archive(source, [(name, encoded)])

    else:
        source.write_bytes(encoded)

    original = read_file_state(source)
    expected = create_expected_rom(source, name=name, platform=platform, canonical=canonical, encoded=encoded)
    dat_rule = libretro_dat_rule(expected)
    snapshot_url = get_snapshot_url(platform, "Canonical (USA)")
    rules = [
        dat_rule, *thumbnail_index_rules(platform, names=["Canonical (USA).png"]),
        f"GET {snapshot_url} -> 200 :".encode() + png,
    ]
    pipeline = build_provider(HttpClient(options.cache), environ={}, source_names=["libretro"])

    with response_mock(rules):
        result = run(replace(options, fix_nes_headers=False), pipeline)

    assert result.imported == result.total == 1
    assert result.removed == result.rejected == result.missing_thumbnails == 0
    assert read_file_state(source) == original
    entry = decode((options.dst / "catalogue.json").read_bytes())[0]
    digest = expected.entry["hash"]
    assert entry["hash"] == digest
    assert entry["image"] == f"{platform}/{digest[:2]}/{name}"
    assert entry["title"] == Path(name).stem
    image = options.dst / "images" / entry["image"]
    assert image.read_bytes() == encoded
    inspected = inspect_rom(image, image.name)
    assert inspected.full_lookup == inspected.lookups[0] == expected.full_lookup
    assert inspected.lookups[-1] == expected.lookups[-1]
    assert (options.dst / "thumbs" / get_thumbnail_name(digest)).read_bytes() == convert(png)
    journal = json.loads(next((options.logs / "sync").rglob("journal.json")).read_text())
    assert journal["metadata_fields"] == {digest: {"thumbnail": "libretro"}}
    assert [journal["source_urls"][ref] for ref in journal["metadata_sources"][digest]] == [
        dat_rule.split()[1], snapshot_url,
    ]
    normalized = [
        event for event in journal["diagnostics"]["events"]
        if event["kind"] == "identity" and event["outcome"] == "normalized_lookup"
    ]
    assert len(normalized) == 1
    event = normalized[0]
    assert event["sha256"] == digest
    assert (event["sha1"], event["size"]) == expected.lookups[-1]
    assert event["crc32"] == expected.crc32s[expected.lookups[-1]]
    assert event["transformation"]


@pytest.mark.parametrize("warm_cache", [False, True])
def test_metadata_fills_snapshots_without_rewriting_library(
    options: Options, provider: Any, lookup_image: Callable[[str], tuple[str, str, bytes, bytes]],
    libretro_dat_rule: Callable[..., str], thumbnail_index_rules: Callable[..., list[str]],
    response_mock: Callable[..., Any], png: bytes, warm_cache: bool,
) -> None:
    prepared = {}

    for kind, title in (("n64", "Missing (U) [!]"), ("snes", "Exact (USA)"), ("smd", "Existing (U) [!]")):
        platform, suffix, canonical, encoded = lookup_image(kind)
        source = options.src / f"{title}{suffix}"
        source.write_bytes(encoded)
        prepared[platform] = create_expected_rom(
            source, name=source.name, platform=platform, canonical=canonical, encoded=encoded,
        )

    run(replace(options, fix_nes_headers=False), provider)

    if not warm_cache:
        (options.cache / IdentityCache.FILE_NAME).unlink()

    catalogue = options.dst / "catalogue.json"
    entries = {entry["platform"]: entry for entry in decode(catalogue.read_bytes())}
    existing = entries["MD"]["hash"]
    existing_thumbnail = options.dst / "thumbs" / get_thumbnail_name(existing)
    existing_thumbnail.parent.mkdir()
    existing_thumbnail.write_bytes(png)
    preferences = options.dst.parent / "prefs.json"
    preferences.write_bytes(b"{\"favorites\":[],\"custom\":true}\n")
    protected = [catalogue, existing_thumbnail, preferences]
    protected.extend(rom.path for rom in prepared.values())
    protected.extend(options.dst / "images" / entry["image"] for entry in entries.values())

    for directory, extension in (("saves", "srm"), ("states", "state")):
        path = options.dst / directory / f"{entries["N64"]["hash"]}.{extension}"
        path.parent.mkdir()
        path.write_bytes(f"Synthetic persistent {directory}".encode())
        protected.append(path)

    original = {path: read_file_state(path) for path in protected}
    canonical_rom, fallback_rom = prepared["N64"], prepared["SNES"]
    canonical_url = get_snapshot_url("N64", "Canonical (USA)")
    fallback_name = Path(fallback_rom.entry["image"]).stem
    fallback_url = get_snapshot_url("SNES", fallback_name)
    fallback_rule = libretro_dat_rule(fallback_rom, title="Unrelated (USA)").replace(
        f"sha1 {fallback_rom.lookups[-1][0]}", f"sha1 {"0" * 40}",
    )
    rules = [
        libretro_dat_rule(canonical_rom), fallback_rule,
        *thumbnail_index_rules("N64", names=["Canonical (USA).png"]),
        *thumbnail_index_rules("SNES", names=[f"{fallback_name}.png"]),
        f"GET {canonical_url} -> 200 :".encode() + png,
        f"GET {fallback_url} -> 200 :".encode() + png,
    ]
    pipeline = build_provider(HttpClient(options.cache), environ={}, source_names=["libretro"])

    with response_mock(rules) as mock:
        result = run(replace(options, src=None, fix_nes_headers=False), pipeline)
        requested = [call.request.url for call in mock.calls]

    assert result.total == 3
    assert result.imported == result.removed == result.rejected == result.missing_thumbnails == 0
    assert requested.count(rules[0].split()[1]) == requested.count(fallback_rule.split()[1]) == 1

    for path, state in original.items():
        assert read_file_state(path) == state

    for platform in ("N64", "SNES"):
        thumbnail = options.dst / "thumbs" / get_thumbnail_name(entries[platform]["hash"])
        assert thumbnail.read_bytes() == convert(png)

    journal = json.loads(next((options.logs / "metadata").rglob("journal.json")).read_text())
    assert journal["metadata_fields"] == {
        entries[platform]["hash"]: {"thumbnail": "libretro"} for platform in ("N64", "SNES")
    }
    normalized = {
        event["sha256"]: event for event in journal["diagnostics"]["events"]
        if event["kind"] == "identity" and event["outcome"] == "normalized_lookup"
    }
    expected_normalized = set() if warm_cache else {entries[platform]["hash"] for platform in ("N64", "SNES")}
    assert set(normalized) == expected_normalized
    expected_by_hash = {rom.entry["hash"]: rom for rom in prepared.values()}

    for digest, event in normalized.items():
        expected = expected_by_hash[digest]
        assert (event["sha1"], event["size"]) == expected.lookups[-1]
        assert event["crc32"] == expected.crc32s[expected.lookups[-1]]

    for platform in ("N64", "SNES"):
        entry = entries[platform]
        cached = IdentityCache(options.cache).get(options.dst / "images" / entry["image"], entry)
        assert cached is not None
        assert cached.lookups == prepared[platform].lookups
        assert cached.crc32s == prepared[platform].crc32s

    assert journal["diagnostics"]["counts"]["libretro"]["thumbnail.filename_fallback"] == 1
    fallback_sources = journal["metadata_sources"][entries["SNES"]["hash"]]
    assert [journal["source_urls"][ref] for ref in fallback_sources] == [fallback_url]
