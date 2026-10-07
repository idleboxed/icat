"""Synthetic 32X cartridges; no proprietary ROMs or live metadata services.

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

import pytest

from icat.catalogue.codec import decode
from icat.cli import main
from icat.metadata.provider import build_provider
from icat.io.http import HttpClient
from icat.roms.discovery import discover_sources
from icat.roms.inspection import inspect_rom
from icat.roms.staging import stage_source
from icat.metadata.sources.openvgdb import URL
from icat.operations.options import Options
from icat.operations.session import run


@pytest.fixture
def cartridge() -> bytes:
    data = bytearray(2048)
    data[0x100:0x108] = b"SEGA 32X"
    data[0x3c0:0x3c4] = b"MARS"
    return bytes(data)


@pytest.mark.parametrize("container", ["32x", "32X", "zip", "7z"])
@pytest.mark.parametrize("move", [False, True])
def test_32x_import_preserves_bytes_and_obeys_copy_move(
    options: Options, provider: Any, sevenzip_archive: Callable[..., Path], cartridge: bytes,
    container: str, move: bool,
) -> None:
    path = options.src / f"Game.{container}"
    name = path.name if container.lower() == "32x" else "Game.32X"

    if container == "zip":

        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(f"nested/{name}", cartridge)

    elif container == "7z":
        sevenzip_archive(path, [(f"nested/{name}", cartridge)])

    else:
        path.write_bytes(cartridge)

    original = path.read_bytes()

    result = run(replace(options, move_roms=move, platforms=("32X",)), provider)

    assert result.imported == result.total == 1
    assert result.removed == int(move)
    assert result.rejected == 0
    entry = decode((options.dst / "catalogue.json").read_bytes())[0]
    digest = hashlib.sha256(cartridge).hexdigest()
    assert entry["platform"] == "32X"
    assert entry["hash"] == digest
    assert entry["image"] == f"32X/{digest[:2]}/{name}"
    assert (options.dst / "images" / entry["image"]).read_bytes() == cartridge

    if move:
        assert not path.exists()

    else:
        assert path.read_bytes() == original


def test_32x_lookup_uses_full_bytes_without_header_stripping(tmp_path: Path, cartridge: bytes) -> None:
    path = tmp_path / "Game.32x"
    path.write_bytes(cartridge)

    rom = inspect_rom(path, path.name)

    assert rom.entry["platform"] == "32X"
    assert rom.lookups == [(hashlib.sha1(cartridge).hexdigest(), len(cartridge))]
    assert rom.full_lookup == rom.lookups[0]
    assert rom.crc32s == {rom.lookups[0]: f"{zlib.crc32(cartridge):08x}"}


def test_32x_platform_filter_keeps_mega_drive_sources(options: Options, provider: Any, cartridge: bytes) -> None:
    selected = options.src / "Game.32x"
    selected.write_bytes(cartridge)
    other = options.src / "Other.gen"
    other.write_bytes(b"Synthetic Mega Drive")

    assert discover_sources(options.src, platforms=("32X",)).candidates == [selected]
    assert stage_source(selected, options.dst, platforms=("MD",)) is None
    result = run(replace(options, move_roms=True, platforms=("32X",)), provider)

    assert result.removed == 1
    assert not selected.exists()
    assert other.read_bytes() == b"Synthetic Mega Drive"


def test_32x_move_keeps_source_when_publication_fails(
    options: Options, provider: Any, cartridge: bytes, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = options.src / "Game.32x"
    path.write_bytes(cartridge)

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("Synthetic publication failure")

    monkeypatch.setattr("icat.operations.publication.publish_catalogue", fail)

    with pytest.raises(OSError, match="Synthetic publication failure"):
        run(replace(options, move_roms=True), provider)

    assert path.read_bytes() == cartridge


@pytest.mark.parametrize("platform", [30, 29])
def test_hasheous_32x_requires_its_own_platform(
    tmp_path: Path, cartridge: bytes, response_mock: Callable[..., Any], platform: int,
) -> None:
    path = tmp_path / "Game.32x"
    path.write_bytes(cartridge)
    rom = inspect_rom(path, path.name)
    digest, size = rom.lookups[0]
    body = {
        "name": "Synthetic 32X Game",
        "platform": {"metadata": [{"objectType": "Platform", "id": f"{platform}",
                                   "status": "Mapped", "source": "IGDB"}]},
        "signature": {"rom": {"sha1": digest, "size": size}, "game": {}},
    }
    pipeline = build_provider(HttpClient(tmp_path / "cache"), environ={}, source_names=["hasheous"])

    with response_mock(f"GET https://hasheous.org/api/v1/Lookup/ByHash/sha1/{digest} -> 200 :{json.dumps(body)}"):
        result = pipeline.lookup(rom)

    assert result.fields == ({"title": "Synthetic 32X Game"} if platform == 30 else {})


@pytest.mark.parametrize("platform", [29, 33])
def test_openvgdb_32x_requires_its_own_system(
    tmp_path: Path, cartridge: bytes, openvgdb_archive: Callable[..., bytes],
    response_mock: Callable[..., Any], platform: int,
) -> None:
    path = tmp_path / "Game.32x"
    path.write_bytes(cartridge)
    rom = inspect_rom(path, path.name)
    digest, size = rom.lookups[0]
    data = openvgdb_archive(digest=digest, size=size, platform=platform)
    pipeline = build_provider(HttpClient(tmp_path / "cache"), environ={}, source_names=["openvgdb"])

    with response_mock(f"GET {URL} -> 200 :".encode() + data):
        result = pipeline.lookup(rom)

    assert bool(result.fields) == (platform == 29)


def test_cli_accepts_32x_platform(options: Options, cartridge: bytes) -> None:
    (options.src / "Game.32x").write_bytes(cartridge)

    result = main([
        "sync", "--http-offline", "--platform", "32X", "--sources", "hasheous",
        "--src", f"{options.src}", "--dst", f"{options.dst}", "--cache", f"{options.cache}",
    ])

    assert result == 0
    assert decode((options.dst / "catalogue.json").read_bytes())[0]["platform"] == "32X"
