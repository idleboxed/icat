
import errno
import hashlib
import os
import struct
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from ctypes import CDLL
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from icat.errors import CatalogueError, SourceError
from icat.roms.formats.chd import detect_platform
from icat.roms.formats.chd_reader import FRAME_BYTES, ChdReader, load_library
from icat.io.files import open_regular_file
from icat.roms.discovery import discover_sources
from icat.roms.inspection import inspect_rom
from icat.roms.platforms import get_selected_extensions
from icat.operations.options import Options
from icat.operations.session import run
from icat.roms.formats import chd_reader


@pytest.mark.parametrize("platform", ["PSX", "SCD"])
@pytest.mark.parametrize("mode", ["MODE1", "MODE1_RAW", "MODE2", "MODE2_FORM1", "MODE2_RAW"])
@pytest.mark.parametrize("pregap,stored", [(0, False), (150, False), (150, True)])
def test_detects_platform_from_service_sectors(
    tmp_path: Path, native_chd: CDLL, chd_image: Callable[..., bytes],
    platform: str, mode: str, pregap: int, stored: bool,
) -> None:
    path = tmp_path / "misleading-saturn.chd"
    path.write_bytes(chd_image(platform, mode=mode, pregap=pregap, stored=stored))

    assert detect_platform(path) == platform

@pytest.mark.parametrize("version", [3, 4])
@pytest.mark.parametrize("platform", ["PSX", "SCD"])
def test_legacy_headers_and_track_metadata(
    tmp_path: Path, native_chd: CDLL, chd_image: Callable[..., bytes], version: int, platform: str,
) -> None:
    metadata = b"TRACK:1 TYPE:MODE1 SUBTYPE:NONE FRAMES:32"
    data = chd_image(platform, mode="MODE1", version=version, metadata=metadata).replace(b"CHT2", b"CHTR", 1)
    path = tmp_path / "legacy.chd"
    path.write_bytes(data)

    assert detect_platform(path) == platform

@pytest.mark.parametrize("platform", ["PSX", "SCD"])
def test_compressed_chd_has_container_identity_without_false_track_hashes(
    tmp_path: Path, native_chd: CDLL, compressed_chd: dict[str, bytes], platform: str,
) -> None:
    path = tmp_path / "Game (Japan).CHD"
    data = compressed_chd[platform]
    path.write_bytes(data)

    rom = inspect_rom(path, path.name)

    assert rom.entry["platform"] == platform
    assert rom.entry["hash"] == hashlib.sha256(data).hexdigest()
    assert rom.lookups == []
    assert rom.crc32s == {}
    assert rom.full_lookup is None
    assert path.read_bytes() == data

def test_shared_extension_discovery_never_guesses_a_platform(tmp_path: Path) -> None:
    path = tmp_path / "game.CHD"
    path.touch()

    assert get_selected_extensions(("PSX",)) == get_selected_extensions(("SCD",)) == {".chd": None}
    assert ".chd" not in get_selected_extensions(("NES",))
    assert discover_sources(tmp_path, platforms=("PSX",)).candidates == [path]
    assert discover_sources(tmp_path, platforms=("NES",)).candidates == []

@pytest.mark.parametrize("platform", ["unknown", "both"])
def test_undetermined_chd_is_rejected_without_manual_platform_override(
    options: Options, provider: Any, native_chd: CDLL, chd_image: Callable[..., bytes], platform: str,
) -> None:
    source = options.src / "PlayStation.chd"
    data = chd_image(platform)
    source.write_bytes(data)

    result = run(replace(options, move_roms=True, platforms=("PSX",)), provider)

    assert (result.imported, result.removed, result.rejected) == (0, 0, 1)
    assert source.read_bytes() == data

@pytest.mark.parametrize("offset,value,message", [
    (0, b"NOT_A_CHD", "Invalid CHD header"),
    (8, struct.pack(">I", 125), "header length"),
    (12, struct.pack(">I", 2), "Unsupported CHD version"),
    (32, struct.pack(">Q", 1 << 63), "logical size|map exceeds"),
    (40, struct.pack(">Q", 1 << 63), "map offset"),
    (48, bytes(8), "metadata offset"),
    (56, bytes(4), "hunk size"),
    (56, struct.pack(">I", 1 << 31), "hunk size"),
    (60, struct.pack(">I", 512), "not a CD image"),
    (104, b"P" * 20, "Parent-dependent"),
])
def test_invalid_headers_rejected_before_native_load(
    tmp_path: Path, chd_image: Callable[..., bytes], monkeypatch: pytest.MonkeyPatch,
    offset: int, value: bytes, message: str,
) -> None:
    data = bytearray(chd_image())
    data[offset:offset + len(value)] = value
    path = tmp_path / "bad.chd"
    path.write_bytes(data)

    def reject_unexpected_call() -> None:
        pytest.fail("Invalid header must not reach libchdr")

    monkeypatch.setattr(chd_reader, "load_library", reject_unexpected_call)

    with pytest.raises(SourceError, match=message):
        detect_platform(path)

@pytest.mark.parametrize("metadata,message", [
    (b"", "track metadata"),
    (b"TRACK:2 TYPE:MODE1 SUBTYPE:NONE FRAMES:32", "track metadata"),
    (b"TRACK:1 TYPE:AUDIO SUBTYPE:NONE FRAMES:32", "data mode"),
    (b"TRACK:1 TYPE:MODE1 SUBTYPE:NONE FRAMES:2", "too short"),
    (b"TRACK:1 TYPE:MODE1 SUBTYPE:NONE FRAMES:999999", "too short"),
    (b"T" * 1025, "Oversized"),
])
def test_invalid_track_metadata(
    tmp_path: Path, native_chd: CDLL, chd_image: Callable[..., bytes], metadata: bytes, message: str,
) -> None:
    path = tmp_path / "bad.chd"
    path.write_bytes(chd_image(metadata=metadata).replace(b"CHT2", b"CHTR", 1))

    with pytest.raises(SourceError, match=message):
        detect_platform(path)

def test_corrupt_compressed_map_is_rejected_before_native_load(
    tmp_path: Path, compressed_chd: dict[str, bytes],
) -> None:
    data = bytearray(compressed_chd["PSX"])
    offset = struct.unpack_from(">Q", data, 40)[0]
    data[offset:offset + 4] = b"\xff" * 4
    path = tmp_path / "bad.chd"
    path.write_bytes(data)

    with pytest.raises(SourceError, match="compressed hunk map"):
        detect_platform(path)

def test_missing_library_is_actionable_and_does_not_break_cartridge_import(
    tmp_path: Path, nes: bytes, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_missing_library(*args: Any) -> None:
        raise OSError("missing")

    def find_library(name: str) -> None:
        return None

    load_library.cache_clear()
    monkeypatch.setattr(chd_reader.ct, "CDLL", raise_missing_library)
    monkeypatch.setattr(chd_reader, "find_library", find_library)

    with pytest.raises(CatalogueError, match="libchdr0") as error:
        load_library()

    assert type(error.value) is CatalogueError
    path = tmp_path / "working.nes"
    path.write_bytes(nes)
    assert inspect_rom(path, path.name).entry["platform"] == "NES"

def test_probe_uses_bounded_service_blocks_not_the_entire_disc(
    tmp_path: Path, native_chd: CDLL, chd_image: Callable[..., bytes], monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "large.chd"
    path.write_bytes(chd_image())

    with path.open("ab") as stream:
        stream.truncate(700 * 1024 * 1024)  # Sparse synthetic file, no 700 MiB allocation/write.

    reads = []

    @contextmanager
    def track_reader(path: Path) -> Iterator[Any]:

        with open_regular_file(path) as stream:
            class Recording:
                def fileno(self) -> int:
                    return stream.fileno()

                def seek(self, *args: Any) -> int:
                    return stream.seek(*args)

                def read(self, count: int) -> bytes:
                    position = stream.tell()
                    data = stream.read(count)
                    reads.append((position, len(data)))
                    return data

            yield Recording()

    monkeypatch.setattr(chd_reader, "open_regular_file", track_reader)

    assert detect_platform(path) == "PSX"
    assert sum(size for _position, size in reads) < 80_000
    assert all(position + size < 100_000 for position, size in reads)

@pytest.mark.parametrize("budget", ["MAX_READ_BYTES", "MAX_READ_CALLS"])
def test_native_reads_obey_probe_budget_and_close_descriptor(
    tmp_path: Path, native_chd: CDLL, chd_image: Callable[..., bytes], monkeypatch: pytest.MonkeyPatch, budget: str,
) -> None:
    path = tmp_path / "game.chd"
    path.write_bytes(chd_image())
    reader = ChdReader(path)
    monkeypatch.setattr(chd_reader, budget, 140 if budget == "MAX_READ_BYTES" else 2)

    with pytest.raises(SourceError, match="bounded read budget"):

        with reader:
            reader.read_frame(16)

    assert reader.stream.closed
    assert not reader.handle

@pytest.mark.parametrize("failure", [OSError("read failed"), KeyboardInterrupt()])
def test_callback_failures_propagate_and_native_handle_is_closed(
    tmp_path: Path, native_chd: CDLL, chd_image: Callable[..., bytes],
    monkeypatch: pytest.MonkeyPatch, failure: BaseException,
) -> None:
    path = tmp_path / "game.chd"
    path.write_bytes(chd_image())
    reader = ChdReader(path)
    fd = None

    with pytest.raises(type(failure), match="read failed" if isinstance(failure, OSError) else "^$"):

        with reader:
            fd = reader.stream.fileno()

            def raise_failure(*args: Any) -> None:
                raise failure

            monkeypatch.setattr(reader.stream, "read", raise_failure)
            reader.read_frame(16)

    assert not reader.handle

    with pytest.raises(OSError, match=rf"\[Errno {errno.EBADF}\]"):
        os.fstat(fd)

def test_reader_reuses_hunk_and_rejects_out_of_range_frames(
    tmp_path: Path, native_chd: CDLL, chd_image: Callable[..., bytes],
) -> None:
    path = tmp_path / "game.chd"
    path.write_bytes(chd_image())

    with ChdReader(path) as reader:
        reader.read_frame(0)
        before = reader.read_bytes
        assert len(reader.read_frame(1)) == FRAME_BYTES
        assert reader.read_bytes == before

        with pytest.raises(SourceError, match="outside the image"):
            reader.read_frame(32)

        with pytest.raises(SourceError, match="outside the image"):
            reader.read_frame(-1)

    assert reader.stream.closed
    assert not reader.handle

@pytest.mark.parametrize("config,accepted", [
    (b"BOOT2 = cdrom0:\\GAME.ELF;1\r\n", False),
    (b"BOOT = cdrom:\\PSX.EXE;1\nBOOT2 = cdrom0:\\GAME.ELF;1\n", False),
    (b"BOOT = cdrom:\\GAME.EXE;1\r\n", True),
    (b"boot=cdrom:\\GAME.EXE;1\n", True),
    (b"BOOT = host:GAME.EXE\n", False),
    (b"BOOT2 text without assignment\n", False),
])
def test_ps1_boot_config_distinguishes_ps2(
    tmp_path: Path, native_chd: CDLL, chd_image: Callable[..., bytes], config: bytes, accepted: bool,
) -> None:
    data = bytearray(chd_image(mode="MODE1"))
    base = struct.unpack_from(">I", data, 124)[0] * 8 * FRAME_BYTES
    record = base + 20 * FRAME_BYTES
    struct.pack_into("<I", data, record + 10, len(config))
    struct.pack_into(">I", data, record + 14, len(config))
    pos = base + 24 * FRAME_BYTES
    data[pos:pos + len(config)] = config
    path = tmp_path / "not-chosen-by-name.chd"
    path.write_bytes(data)

    if accepted:
        assert detect_platform(path) == "PSX"

    else:

        with pytest.raises(SourceError, match="no PS1 boot signature"):
            detect_platform(path)

@pytest.mark.parametrize("field,offset,value,message", [
    ("pvd", 128, b"\0\x04\x04\0", "logical block size"),
    ("pvd", 156, b"\0", "root directory"),
    ("pvd", 166, struct.pack("<I", 0xffff), "Inconsistent"),
    ("root", 0, b"\x10", "Truncated"),
    ("root", 2, struct.pack("<I", 10000) + struct.pack(">I", 10000), "multi-extent"),
    ("root", 10, struct.pack("<I", 8192) + struct.pack(">I", 8192), "bounded probe limit"),
    ("root", 25, b"\x80", "multi-extent"),
    ("root", 26, b"\x01", "interleaved"),
    ("pvd", 158, struct.pack("<I", 10000) + struct.pack(">I", 10000), "multi-extent"),
])
def test_bad_iso_boot_metadata_is_bounded_and_rejected(
    tmp_path: Path, native_chd: CDLL, chd_image: Callable[..., bytes],
    field: str, offset: int, value: bytes, message: str,
) -> None:
    data = bytearray(chd_image(mode="MODE1"))
    base = struct.unpack_from(">I", data, 124)[0] * 8 * FRAME_BYTES
    pos = base + {"pvd": 16, "root": 20}[field] * FRAME_BYTES + offset
    data[pos:pos + len(value)] = value
    path = tmp_path / "bad.chd"
    path.write_bytes(data)

    with pytest.raises(SourceError, match=message):
        detect_platform(path)

def test_default_psx_exe_is_recognized_without_system_cnf(
    tmp_path: Path, native_chd: CDLL, chd_image: Callable[..., bytes],
) -> None:
    data = bytearray(chd_image(mode="MODE1"))
    base = struct.unpack_from(">I", data, 124)[0] * 8 * FRAME_BYTES
    record = base + 20 * FRAME_BYTES
    data[record + 32] = len(b"PSX.EXE;1")
    data[record + 33:record + 42] = b"PSX.EXE;1"
    pos = base + 24 * FRAME_BYTES
    data[pos:pos + 8] = b"PS-X EXE"
    path = tmp_path / "homebrew.chd"
    path.write_bytes(data)

    assert detect_platform(path) == "PSX"

def test_cyclic_metadata_chain_hits_read_limit_without_hanging(
    tmp_path: Path, native_chd: CDLL, chd_image: Callable[..., bytes], monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = bytearray(chd_image())
    metadata = struct.unpack_from(">Q", data, 48)[0]
    data[metadata:metadata + 4] = b"TEST"
    struct.pack_into(">Q", data, metadata + 8, metadata)
    path = tmp_path / "cycle.chd"
    path.write_bytes(data)
    monkeypatch.setattr(chd_reader, "MAX_READ_CALLS", 32)

    with pytest.raises(SourceError, match="bounded read budget"):
        detect_platform(path)

@pytest.mark.parametrize("version", [3, 4])
def test_legacy_parent_flag_is_rejected_before_library_load(
    tmp_path: Path, chd_image: Callable[..., bytes], monkeypatch: pytest.MonkeyPatch, version: int,
) -> None:
    data = bytearray(chd_image(version=version))
    struct.pack_into(">I", data, 16, 1)
    path = tmp_path / "child.chd"
    path.write_bytes(data)

    def reject_unexpected_call() -> None:
        pytest.fail("Parent image must not reach libchdr")

    monkeypatch.setattr(chd_reader, "load_library", reject_unexpected_call)

    with pytest.raises(SourceError, match="Parent-dependent"):
        detect_platform(path)

def test_unsupported_native_codec_is_a_recoverable_source_error(
    tmp_path: Path, native_chd: CDLL, compressed_chd: dict[str, bytes],
) -> None:
    data = bytearray(compressed_chd["PSX"])
    data[16:20] = b"xxxx"
    path = tmp_path / "codec.chd"
    path.write_bytes(data)

    with pytest.raises(SourceError, match="installed libchdr"):
        detect_platform(path)

def test_chd_open_refuses_symlinks(tmp_path: Path, native_chd: CDLL, chd_image: Callable[..., bytes]) -> None:
    path = tmp_path / "game.chd"
    path.write_bytes(chd_image())
    link = tmp_path / "link.chd"
    link.symlink_to(path)

    with pytest.raises(OSError, match=rf"\[Errno {errno.ELOOP}\]"):
        detect_platform(link)
