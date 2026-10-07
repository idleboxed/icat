"""Synthetic MD identity regressions; no commercial ROM bytes or network.

SPDX-License-Identifier: BSD-3-Clause
"""

import hashlib
import json
import zipfile
import zlib
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from icat.errors import CatalogueError, SourceError
from icat.catalogue.codec import decode
from icat.databases.libretro import BASE, MD_SYSTEM
from icat.roms.formats.mega_drive import MegaDriveIdentifier
from icat.metadata.provider import Provider, build_provider
from icat.io.http import HttpClient
from icat.roms.inspection import inspect_rom
from icat.operations.options import Options
from icat.operations.session import run


@pytest.fixture
def md_bytes() -> bytes:
    # Deliberately no standard SEGA signature. Identification must use all bytes.
    return bytes.fromhex("00fffffe00000200") + b"Synthetic cartridge data" * 32


@pytest.fixture
def md_dat(md_bytes: bytes) -> str:
    return (
        f"clrmamepro ( name \"{MD_SYSTEM}\" )\n"
        f"game ( name \"Unrelated canonical title\" rom ( name \"Unrelated.md\" "
        f"sha1 {hashlib.sha1(md_bytes).hexdigest()} size {len(md_bytes)} "
        f"crc {zlib.crc32(md_bytes):08x} ) )\n"
    )


def create_pipeline(
    cache: Path, *, offline: bool = False, sources: Sequence[str] = (), refresh: bool = False,
) -> Provider:
    return build_provider(
        HttpClient(cache, offline=offline), environ={}, source_names=sources, refresh_databases=refresh,
    )


def build_dat_response(data: str) -> str:
    return f"GET {MegaDriveIdentifier.url} -> 200 :{data}"


@pytest.mark.parametrize("container", ["bin", "zip", "7z"])
@pytest.mark.parametrize("move", [False, True])
def test_verified_bin_import_preserves_bytes_and_verified_move(
    options: Options, md_bytes: bytes, md_dat: str, sevenzip_archive: Callable[..., Path],
    response_mock: Callable[..., Any], container: str, move: bool,
) -> None:
    path = options.src / f"Game.{container}"

    if container == "zip":

        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("Game.bin", md_bytes)

    elif container == "7z":
        sevenzip_archive(path, [("Game.bin", md_bytes)])

    else:
        path.write_bytes(md_bytes)

    original = path.read_bytes()

    with response_mock(build_dat_response(md_dat)) as mock:
        result = run(replace(options, move_roms=move), create_pipeline(options.cache))
        assert len(mock.calls) == 1

    assert (result.imported, result.total, result.rejected, result.removed) == (1, 1, 0, int(move))
    entry = decode((options.dst / "catalogue.json").read_bytes())[0]
    assert entry["platform"] == "MD"
    assert entry["hash"] == hashlib.sha256(md_bytes).hexdigest()
    assert (options.dst / "images" / entry["image"]).read_bytes() == md_bytes

    if move:
        assert not path.exists()

    else:
        assert path.read_bytes() == original

    journal = json.loads(result.journal_path.read_text())
    event = next(entry for entry in journal["diagnostics"]["events"] if entry["outcome"] == "verified_mega_drive")
    assert event["basis"] == "full_file_sha1_size_crc32"
    assert event["database_url"] == MegaDriveIdentifier.url
    assert event["sha256"] == entry["hash"]


@pytest.mark.parametrize("mismatch", [
    "sha1", "size", "crc", "platform", "crc-only", "crc-collision", "conflicting-crc", "multi-rom", "truncated",
])
def test_unverified_bin_is_rejected_and_never_moved(
    options: Options, md_bytes: bytes, md_dat: str, response_mock: Callable[..., Any], mismatch: str,
) -> None:
    path = options.src / "Game.bin"
    path.write_bytes(md_bytes)
    sha1, size, crc = hashlib.sha1(md_bytes).hexdigest(), len(md_bytes), f"{zlib.crc32(md_bytes):08x}"
    old, new = {
        "sha1": (sha1, "0" * 40),
        "size": (f"size {size}", "size 1"),
        "crc": (crc, "12345678"),
        "platform": (MD_SYSTEM, "Different platform"),
        "crc-only": (f"sha1 {sha1} size {size}", ""),
        "crc-collision": ("", ""),
        "conflicting-crc": ("", ""),
        "multi-rom": (" ) )", " ) rom ( name extra.bin ) )"),
        "truncated": ("", ""),
    }[mismatch]
    data = md_dat.replace(old, new) if old else md_dat

    if mismatch == "crc-collision":
        data += f"game ( rom ( sha1 {"0" * 40} size 1 crc {crc} ) )"

    elif mismatch == "conflicting-crc":
        data += f"game ( rom ( sha1 {sha1} size {size} crc 12345678 ) )"

    elif mismatch == "truncated":
        data = data[:-4]

    with response_mock(build_dat_response(data)):
        result = run(replace(options, move_roms=True), create_pipeline(options.cache))

    assert (result.imported, result.rejected, result.removed) == (0, 1, 0)
    assert path.read_bytes() == md_bytes
    assert decode((options.dst / "catalogue.json").read_bytes()) == []


@pytest.mark.parametrize("offset", [0x101, 0x108, None])
def test_nonstandard_header_requires_exact_whole_file_identity(
    tmp_path: Path, md_bytes: bytes, md_dat: str, response_mock: Callable[..., Any], offset: int | None,
) -> None:
    data = bytearray(md_bytes)

    if offset is not None:
        data[offset:offset + 4] = b"SEGA"

    path = tmp_path / "different-name.bin"
    path.write_bytes(data)
    sha1, crc = hashlib.sha1(data).hexdigest(), f"{zlib.crc32(data):08x}"
    md_dat = md_dat.replace(hashlib.sha1(md_bytes).hexdigest(), sha1).replace(f"{zlib.crc32(md_bytes):08x}", crc)
    provider = create_pipeline(tmp_path / "cache")

    with response_mock(build_dat_response(md_dat)):
        rom = inspect_rom(path, path.name, md_identifier=provider.md_identifier)

    assert rom.entry["platform"] == "MD"
    assert rom.entry["hash"] == hashlib.sha256(data).hexdigest()
    assert path.read_bytes() == data


def test_standard_signature_needs_no_database(
    options: Options, md_bytes: bytes, response_mock: Callable[..., Any],
) -> None:
    data = bytearray(md_bytes)
    data[0x100:0x104] = b"SEGA"
    (options.src / "Game.bin").write_bytes(data)

    with response_mock([]) as mock:
        result = run(options, create_pipeline(options.cache))
        assert not mock.calls

    assert result.imported == 1
    assert not (options.cache / "datasets").exists()


def test_offline_cache_revalidates_stored_bin_after_source_move(
    options: Options, md_bytes: bytes, md_dat: str, response_mock: Callable[..., Any],
) -> None:
    path = options.src / "Game.bin"
    path.write_bytes(md_bytes)

    with response_mock(build_dat_response(md_dat)):
        run(replace(options, move_roms=True), create_pipeline(options.cache))

    before = (options.dst / "catalogue.json").read_bytes()

    with response_mock([]) as mock:
        result = run(replace(options, src=None), create_pipeline(options.cache, offline=True))
        assert not mock.calls

    assert not path.exists()
    assert result.total == 1
    assert not result.catalogue_changed
    assert (options.dst / "catalogue.json").read_bytes() == before


@pytest.mark.parametrize("state", ["missing", "corrupt", "unavailable"])
def test_missing_or_unusable_database_never_authorizes_bin(
    options: Options, md_bytes: bytes, md_dat: str, response_mock: Callable[..., Any], state: str,
) -> None:
    path = options.src / "Game.bin"
    path.write_bytes(md_bytes)

    if state == "corrupt":

        with response_mock(build_dat_response(md_dat)):
            run(options, create_pipeline(options.cache))

        manifest = next((options.cache / "datasets").glob("*/current.json"))
        blob = manifest.parent / json.loads(manifest.read_text())["sha256"]
        blob.write_bytes(b"broken cache")

    provider = create_pipeline(options.cache, offline=state != "unavailable")
    responses = [f"GET {MegaDriveIdentifier.url} -> 503 :unavailable"] if state == "unavailable" else []

    with response_mock(responses):

        with pytest.raises(SourceError, match="Ambiguous .bin"):
            inspect_rom(path, path.name, md_identifier=provider.md_identifier)

    assert path.read_bytes() == md_bytes


def test_import_and_players_share_the_same_dat_download(
    options: Options, md_bytes: bytes, md_dat: str, response_mock: Callable[..., Any],
) -> None:
    (options.src / "Game.bin").write_bytes(md_bytes)
    crc = f"{zlib.crc32(md_bytes):08x}"
    users = f"clrmamepro ( name \"{MD_SYSTEM}\" ) game ( users 2 rom ( crc {crc} ) )"
    responses = [build_dat_response(md_dat), f"GET {BASE}/maxusers/{quote(MD_SYSTEM)}.dat -> 200 :{users}"]

    with response_mock(responses) as mock:
        result = run(options, create_pipeline(options.cache, sources=("libretro-players",)))
        assert len(mock.calls) == 2

    assert result.imported == 1
    assert decode((options.dst / "catalogue.json").read_bytes())[0]["players"] == 2


def test_failed_destination_check_retains_verified_source(
    options: Options, md_bytes: bytes, md_dat: str, response_mock: Callable[..., Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = options.src / "Game.bin"
    path.write_bytes(md_bytes)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise CatalogueError("Synthetic destination conflict")

    monkeypatch.setattr("icat.operations.metadata.check_existing", fail)

    with response_mock(build_dat_response(md_dat)):

        with pytest.raises(CatalogueError, match="Synthetic destination conflict"):
            run(replace(options, move_roms=True), create_pipeline(options.cache))

    assert path.read_bytes() == md_bytes


def test_platform_filter_does_not_identify_excluded_bin(
    options: Options, md_bytes: bytes, response_mock: Callable[..., Any],
) -> None:
    path = options.src / "Game.bin"
    path.write_bytes(md_bytes)

    with response_mock([]) as mock:
        result = run(replace(options, platforms=("NES",), move_roms=True), create_pipeline(options.cache))
        assert not mock.calls

    assert result.imported == result.removed == 0
    assert path.read_bytes() == md_bytes


def test_changed_bytes_do_not_match_a_previously_verified_identity(
    tmp_path: Path, md_bytes: bytes, md_dat: str, response_mock: Callable[..., Any],
) -> None:
    path = tmp_path / "Game.bin"
    path.write_bytes(md_bytes)
    provider = create_pipeline(tmp_path / "cache")

    with response_mock(build_dat_response(md_dat)):
        inspect_rom(path, path.name, md_identifier=provider.md_identifier)

    path.write_bytes(md_bytes[:-1] + b"X")

    with response_mock([]) as mock:

        with pytest.raises(SourceError, match="Ambiguous .bin"):
            inspect_rom(path, path.name, md_identifier=provider.md_identifier)

        assert not mock.calls


def test_failed_refresh_keeps_previous_verified_md_database(
    tmp_path: Path, md_bytes: bytes, md_dat: str, response_mock: Callable[..., Any],
) -> None:
    path = tmp_path / "Game.bin"
    path.write_bytes(md_bytes)
    cache = tmp_path / "cache"

    with response_mock(build_dat_response(md_dat)):
        inspect_rom(path, path.name, md_identifier=create_pipeline(cache).md_identifier)

    provider = create_pipeline(cache, refresh=True)

    with response_mock(build_dat_response("broken database")):
        rom = inspect_rom(path, path.name, md_identifier=provider.md_identifier)

    assert rom.entry["platform"] == "MD"


def test_provider_construction_does_not_create_or_fetch_database(
    tmp_path: Path, response_mock: Callable[..., Any],
) -> None:
    cache = tmp_path / "not-created"

    with response_mock([]) as mock:
        provider = create_pipeline(cache)
        assert provider.md_identifier is not None
        assert not mock.calls

    assert not cache.exists()
