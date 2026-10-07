"""Synthetic hashes/boards only; no commercial ROMs, network or emulator runs.

SPDX-License-Identifier: BSD-3-Clause
"""

import hashlib
import json
import logging
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from icat.errors import CatalogueError, SourceError
from icat.catalogue.codec import decode
from icat.cli import main
from icat.databases.cache import DatasetCache
from icat.roms.formats.nes_headers import DATASET, URL, Board, NesHeaderFixer
from icat.io.http import HttpClient
from icat.roms.staging import stage_source
from icat.operations.options import Options
from icat.operations.result import Result
from icat.operations.session import run
from icat.operations import publication


@pytest.fixture
def broken_nes(nes: bytes) -> bytes:
    return nes[:5] + b"\x02" + nes[6:]


@pytest.fixture
def board_xml(nes: bytes) -> Callable[..., bytes]:
    def make(
        *, payload: bytes | None = None, crc: str | None = None, prg: str = "16k", chr_size: str = "8k",
        mapper: str = "0", battery: bool = False, extra: str = "",
    ) -> bytes:
        data = nes[16:] if payload is None else payload
        digest = hashlib.sha1(data).hexdigest()
        crc = f"{zlib.crc32(data):08x}" if crc is None else crc
        ram = "<wram size=\"8k\" battery=\"1\" />" if battery else ""
        return (
            f"<database version=\"1.0\"><game><cartridge system=\"Famicom\" sha1=\"{digest}\" crc=\"{crc}\">"
            f"<board mapper=\"{mapper}\"><prg size=\"{prg}\" /><chr size=\"{chr_size}\" />{ram}</board>"
            f"</cartridge></game>{extra}</database>"
        ).encode()

    return make


@pytest.fixture
def cached_fixer(tmp_path: Path, board_xml: Callable[..., bytes]) -> Callable[..., NesHeaderFixer]:
    def make(data: bytes | None = None, *, root: Path | None = None) -> NesHeaderFixer:
        root = tmp_path / "cache" if root is None else root
        data = board_xml() if data is None else data
        folder = root / "datasets" / DATASET
        folder.mkdir(parents=True)
        digest = hashlib.sha256(data).hexdigest()
        (folder / digest).write_bytes(data)
        (folder / "current.json").write_text(json.dumps({"url": URL, "sha256": digest}))
        return NesHeaderFixer(HttpClient(root, offline=True), DatasetCache(root / "datasets", offline=True))

    return make


@pytest.mark.parametrize("container", ["raw", "zip", "7z"])
@pytest.mark.parametrize("battery", [False, True])
def test_verified_size_repair_changes_only_staged_header_and_warns(
    tmp_path: Path, nes: bytes, broken_nes: bytes, board_xml: Callable[..., bytes],
    cached_fixer: Callable[..., NesHeaderFixer], sevenzip_archive: Callable[..., Path],
    caplog: pytest.LogCaptureFixture, container: str, battery: bool,
) -> None:
    fixer = cached_fixer(board_xml(battery=battery))
    source = tmp_path / {"raw": "Game.nes", "zip": "Game.zip", "7z": "Game.7z"}[container]

    if container == "raw":
        source.write_bytes(broken_nes)

    elif container == "zip":

        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("folder/Game.nes", broken_nes)

    else:
        sevenzip_archive(source, [("folder/Game.nes", broken_nes)])

    original = source.read_bytes()
    staging = tmp_path / "stage"
    staging.mkdir()
    expected = bytearray(nes)

    if battery:
        expected[6] |= 2

    result = stage_source(source, staging, header_fixer=fixer)

    rom = result.roms[0]
    assert source.read_bytes() == original
    assert rom.path.read_bytes() == expected
    assert rom.entry["hash"] == hashlib.sha256(expected).hexdigest()
    assert rom.lookups[0] == (hashlib.sha1(nes[16:]).hexdigest(), len(nes) - 16)
    fix = result.header_fixes[0]
    assert fix["original_sha256"] == hashlib.sha256(broken_nes).hexdigest()
    assert fix["sha256"] == rom.entry["hash"]
    assert fix["battery_added"] is battery
    warnings = [record for record in caplog.records if "NES header fixed" in record.getMessage()]
    assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING
    assert warnings[0].getMessage().startswith("\"Game.nes\" — NES header fixed in staging: CHR 16 -> 8 KiB")
    assert "source follows COPY/MOVE mode" in warnings[0].getMessage()


@pytest.mark.parametrize("mismatch", ["hash", "crc", "size", "mapper", "ambiguous", "incomplete"])
def test_unverified_or_ambiguous_payload_remains_rejected(
    tmp_path: Path, broken_nes: bytes, board_xml: Callable[..., bytes],
    cached_fixer: Callable[..., NesHeaderFixer], mismatch: str,
) -> None:

    if mismatch == "hash":
        data = board_xml(payload=b"wrong")

    elif mismatch == "crc":
        data = board_xml(crc="00000000")

    elif mismatch == "size":
        data = board_xml(chr_size="16k")

    elif mismatch == "mapper":
        data = board_xml(mapper="1")

    else:
        other = (
            board_xml(chr_size="16k") if mismatch == "ambiguous"
            else board_xml().replace(b"<chr size=\"8k\" />", b"")
        )
        extra = other.split(b">", 1)[1].rsplit(b"</database>", 1)[0].decode()
        data = board_xml(extra=extra)

    fixer = cached_fixer(data)
    source = tmp_path / "Game.nes"
    source.write_bytes(broken_nes)
    staging = tmp_path / "stage"
    staging.mkdir()

    with pytest.raises(SourceError, match="NES header/data size mismatch"):
        stage_source(source, staging, header_fixer=fixer)

    assert source.read_bytes() == broken_nes
    assert not any(path.read_bytes() != broken_nes for path in staging.iterdir())


@pytest.mark.parametrize("kind", ["valid", "trailing", "nes2", "trainer", "vs", "reserved", "no_magic", "not_nes"])
def test_unneeded_or_unsupported_headers_do_not_access_the_database(
    tmp_path: Path, nes: bytes, broken_nes: bytes, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    fixer = NesHeaderFixer(HttpClient(tmp_path / "cache"), DatasetCache(tmp_path / "cache/datasets"))

    def reject_unexpected_call(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Unnecessary database access")

    monkeypatch.setattr(fixer.datasets, "obtain", reject_unexpected_call)
    data = bytearray(broken_nes)
    name = "Game.nes"

    if kind == "valid":
        data = bytearray(nes)

    elif kind == "trailing":
        data.extend(bytes(16384))

    elif kind == "nes2":
        data[7] = 8

    elif kind == "trainer":
        data[6] |= 4

    elif kind == "vs":
        data[7] = 1

    elif kind == "reserved":
        data[12] = 1

    elif kind == "no_magic":
        data[:4] = bytes(4)

    else:
        name = "Game.bin"

    path = tmp_path / name
    path.write_bytes(data)

    assert fixer.repair_staged(path, name=name) is None

    assert path.read_bytes() == data
    assert not fixer.http.cache.exists()


@pytest.mark.parametrize("data", [
    b"bad", b"<database version=\"2.0\" />", b"<database version=\"1.0\" />",
    b"<!DOCTYPE database [<!ENTITY x \"boom\">]><database version=\"1.0\">&x;</database>",
    "<database version=\"1.0\" />".encode("utf-16"),
])
def test_invalid_database_cannot_enable_repair(
    tmp_path: Path, broken_nes: bytes, cached_fixer: Callable[..., NesHeaderFixer], data: bytes,
) -> None:
    fixer = cached_fixer(data)
    path = tmp_path / "Game.nes"
    path.write_bytes(broken_nes)

    assert fixer.repair_staged(path, name=path.name) is None

    assert path.read_bytes() == broken_nes


def test_missing_offline_database_keeps_original_header(tmp_path: Path, broken_nes: bytes) -> None:
    http = HttpClient(tmp_path / "cache", offline=True)
    fixer = NesHeaderFixer(http, DatasetCache(http.cache / "datasets", offline=True))
    path = tmp_path / "Game.nes"
    path.write_bytes(broken_nes)

    assert fixer.repair_staged(path, name=path.name) is None

    assert path.read_bytes() == broken_nes


@pytest.mark.parametrize("container", ["raw", "zip", "7z"])
@pytest.mark.parametrize("move", [False, True])
def test_repaired_sources_follow_copy_move_policy_after_verified_publication(
    options: Options, provider: Any, nes: bytes, broken_nes: bytes, cached_fixer: Callable[..., NesHeaderFixer],
    sevenzip_archive: Callable[..., Path], container: str, move: bool,
) -> None:
    fixer = cached_fixer(root=options.cache)
    source = options.src / {"raw": "Broken.nes", "zip": "Broken.zip", "7z": "Broken.7z"}[container]

    if container == "raw":
        source.write_bytes(broken_nes)

    elif container == "zip":

        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("Broken.nes", broken_nes)

    else:
        sevenzip_archive(source, [("Broken.nes", broken_nes)])

    original = source.read_bytes()
    good = options.src / "Good.nes"
    good.write_bytes(nes[:-1] + b"Z")

    result = run(replace(options, move_roms=move), provider, header_fixer=fixer)

    assert result.total == 2 and result.removed == 2 * move and result.rejected == 0

    if move:
        assert not source.exists() and not good.exists()

    else:
        assert source.read_bytes() == original and good.is_file()

    entries = decode((options.dst / "catalogue.json").read_bytes())
    repaired = next(entry for entry in entries if entry["hash"] == hashlib.sha256(nes).hexdigest())
    assert (options.dst / "images" / repaired["image"]).read_bytes() == nes
    doc = json.loads(next(options.logs.rglob("journal.json")).read_text())
    assert doc["config"]["fix_nes_headers"] is True
    assert doc["summary"]["nes_headers_fixed"] == 1
    assert "retained_sources" not in doc
    assert doc["nes_header_fixes"][0]["source"] == f"{source}"
    assert doc["nes_header_fixes"][0]["sha256"] == hashlib.sha256(nes).hexdigest()
    assert doc["removed"] == ([f"{source}", f"{good}"] if move else [])

    # COPY followed by MOVE also covers already-indexed repaired ROMs from 0.2.9.
    second = run(replace(options, move_roms=True), provider, header_fixer=fixer)

    assert second.imported == 0 and second.removed == 2 * (not move) and second.total == 2
    assert not source.exists() and not good.exists()


@pytest.mark.parametrize("failure", ["publication", "source_changed", "destination_changed", "cancelled"])
def test_repaired_archive_is_not_removed_when_publication_or_preflight_fails(
    options: Options, provider: Any, nes: bytes, broken_nes: bytes, cached_fixer: Callable[..., NesHeaderFixer],
    sevenzip_archive: Callable[..., Path], monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    fixer = cached_fixer(root=options.cache)
    source = sevenzip_archive(options.src / "Broken.7z", [("Broken.nes", broken_nes)])
    original = source.read_bytes()
    (options.src / "Good.nes").write_bytes(nes[:-1] + b"Z")
    publish = publication.publish_catalogue

    def fail_publication(path: Path, encoded: bytes, *, original: bytes | None) -> None:

        if failure == "publication":
            raise OSError("Synthetic publication failure")

        if failure == "cancelled":
            raise KeyboardInterrupt()

        publish(path, encoded, original=original)

        if failure == "source_changed":

            with source.open("ab") as stream:
                stream.write(b"changed")

        else:
            entry = next(item for item in decode(encoded) if item["hash"] == hashlib.sha256(nes).hexdigest())
            (options.dst / "images" / entry["image"]).write_bytes(b"corrupt")

    monkeypatch.setattr(publication, "publish_catalogue", fail_publication)
    error, message = {
        "publication": (OSError, "Synthetic publication failure"),
        "source_changed": (CatalogueError, "Source changed"),
        "destination_changed": (CatalogueError, "Destination changed"),
        "cancelled": (KeyboardInterrupt, "^$"),
    }[failure]

    with pytest.raises(error, match=message):
        run(replace(options, move_roms=True), provider, header_fixer=fixer)

    assert source.read_bytes() == original + (b"changed" if failure == "source_changed" else b"")
    assert (options.src / "Good.nes").is_file()
    doc = json.loads(next(options.logs.rglob("journal.json")).read_text())
    assert doc["removed"] == []
    assert doc["state"] == ("interrupted" if failure == "cancelled" else "failed")


def test_disabled_fixing_does_not_call_fixer(
    options: Options, provider: Any, broken_nes: bytes,
    cached_fixer: Callable[..., NesHeaderFixer], monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixer = cached_fixer(root=options.cache)

    def reject_unexpected_call(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Fixing is disabled")

    monkeypatch.setattr(fixer, "repair_staged", reject_unexpected_call)
    source = options.src / "Game.nes"
    source.write_bytes(broken_nes)

    result = run(replace(options, fix_nes_headers=False), provider, header_fixer=fixer)

    assert result.rejected == 1 and result.total == 0
    assert source.read_bytes() == broken_nes


@pytest.mark.parametrize("flags, enabled", [([], True), (["--no-fix-nes-headers"], False)])
def test_cli_repair_is_enabled_by_default_and_can_be_disabled(
    monkeypatch: pytest.MonkeyPatch, flags: list[str], enabled: bool,
) -> None:
    captured = []

    def capture(options: Options, provider: Any) -> Result:
        captured.append(options)
        return Result(0, 0, 0, 0, 0)

    monkeypatch.setattr("icat.cli.run", capture)

    assert main(["sync", "--http-offline", *flags]) == 0

    assert captured[0].fix_nes_headers is enabled


def test_cli_repairs_from_cached_database_without_key_or_selected_metadata_sources(
    options: Options, nes: bytes, broken_nes: bytes,
    cached_fixer: Callable[..., NesHeaderFixer], caplog: pytest.LogCaptureFixture,
) -> None:
    cached_fixer(root=options.cache)
    (options.src / "Game.nes").write_bytes(broken_nes)

    result = main([
        "sync", "--src", f"{options.src}", "--dst", f"{options.dst}", "--cache", f"{options.cache}",
        "--http-offline", "--sources", "thegamesdb",
    ])

    assert result == 0
    assert decode((options.dst / "catalogue.json").read_bytes())[0]["hash"] == hashlib.sha256(nes).hexdigest()
    assert "NES header fixed" in caplog.text


def test_header_repair_refuses_staging_mutation_during_database_lookup(
    tmp_path: Path, broken_nes: bytes, cached_fixer: Callable[..., NesHeaderFixer], monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixer = cached_fixer()
    path = tmp_path / "Game.nes"
    path.write_bytes(broken_nes)
    original = fixer.get_database

    def change_rom_during_lookup() -> tuple[Path | None, dict[str, Board | None]]:
        value = original()
        path.write_bytes(broken_nes[:-1] + b"Z")
        return value

    monkeypatch.setattr(fixer, "get_database", change_rom_during_lookup)

    with pytest.raises(CatalogueError, match="Staged .*changed"):
        fixer.repair_staged(path, name=path.name)

    assert path.read_bytes() == broken_nes[:-1] + b"Z"


def test_repair_database_download_uses_existing_transport_and_is_cached_once(
    tmp_path: Path, broken_nes: bytes, board_xml: Callable[..., bytes], response_mock: Callable[..., Any],
) -> None:
    http = HttpClient(tmp_path / "cache")
    fixer = NesHeaderFixer(http, DatasetCache(http.cache / "datasets"))
    paths = [tmp_path / f"Game-{index}.nes" for index in range(2)]

    for path in paths:
        path.write_bytes(broken_nes)

    with response_mock(f"GET {URL} -> 200 :{board_xml().decode()}") as mock:
        results = [fixer.repair_staged(path, name=path.name) for path in paths]

        assert len(mock.calls) == 1

    assert all(result is not None for result in results)
    assert (http.cache / "datasets" / DATASET / "current.json").is_file()


@pytest.mark.parametrize("prg_kib, chr_kib, mapper, battery", [
    (128, 64, 10, True), (32, 16, 1, False), (128, 32, 1, False),
])
def test_reported_header_shapes_are_repaired_with_synthetic_payloads(
    tmp_path: Path, board_xml: Callable[..., bytes], cached_fixer: Callable[..., NesHeaderFixer],
    prg_kib: int, chr_kib: int, mapper: int, battery: bool,
) -> None:
    payload = b"P" * (prg_kib * 1024) + b"C" * (chr_kib * 1024)
    header = b"NES\x1a" + bytes([prg_kib // 16, chr_kib // 4, mapper << 4, 0]) + bytes(8)
    path = tmp_path / "Game.nes"
    path.write_bytes(header + payload)
    fixer = cached_fixer(board_xml(
        payload=payload, prg=f"{prg_kib}k", chr_size=f"{chr_kib}k", mapper=f"{mapper}", battery=battery,
    ))

    result = fixer.repair_staged(path, name=path.name)

    assert result["chr_bytes_before"] == chr_kib * 2048
    assert result["chr_bytes_after"] == chr_kib * 1024
    assert path.read_bytes()[16:] == payload
    assert path.read_bytes()[5] == chr_kib // 8
    assert bool(path.read_bytes()[6] & 2) is battery


@pytest.mark.parametrize("failure", [OSError("Write sync failure"), KeyboardInterrupt()])
def test_failed_header_write_does_not_catalogue_or_remove_source(
    options: Options, provider: Any, broken_nes: bytes, cached_fixer: Callable[..., NesHeaderFixer],
    monkeypatch: pytest.MonkeyPatch, failure: BaseException,
) -> None:
    fixer = cached_fixer(root=options.cache)
    source = options.src / "Game.nes"
    source.write_bytes(broken_nes)
    original = fixer.get_database

    def fail_sync(fd: int) -> None:
        raise failure

    def fail_after_database() -> tuple[Path | None, dict[str, Board | None]]:
        value = original()
        monkeypatch.setattr("icat.roms.formats.nes_headers.os.fsync", fail_sync)
        return value

    monkeypatch.setattr(fixer, "get_database", fail_after_database)

    with pytest.raises(type(failure), match="Write sync failure" if isinstance(failure, OSError) else "^$"):
        run(replace(options, move_roms=True), provider, header_fixer=fixer)

    assert source.read_bytes() == broken_nes
    assert not (options.dst / "catalogue.json").exists()
    assert not list((options.dst / "images").rglob("*.nes"))


def test_hardlinked_staging_copy_cannot_modify_another_file(
    tmp_path: Path, broken_nes: bytes, cached_fixer: Callable[..., NesHeaderFixer],
) -> None:
    fixer = cached_fixer()
    original = tmp_path / "original.nes"
    original.write_bytes(broken_nes)
    path = tmp_path / "staged.nes"
    path.hardlink_to(original)

    with pytest.raises(CatalogueError, match="Staged ROM changed"):
        fixer.repair_staged(path, name=path.name)

    assert original.read_bytes() == broken_nes
