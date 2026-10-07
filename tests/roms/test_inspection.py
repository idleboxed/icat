import hashlib
import zlib
from pathlib import Path
import pytest
from icat.errors import CatalogueError, SourceError
from icat.reporting.diagnostics import Diagnostics
from icat.roms.inspection import inspect_rom


def test_invalid_nes_payload_remains_rejected(tmp_path: Path, nes: bytes) -> None:
    path = tmp_path / "truncated.nes"
    path.write_bytes(nes[:-1])

    with pytest.raises(CatalogueError, match="NES header/data size mismatch"):
        inspect_rom(path, path.name)


def test_nes_trainer_not_part_of_lookup_but_in_identity(tmp_path: Path, nes: bytes) -> None:
    data = bytearray(nes)
    data[6] = 4
    data[16:16] = b"T" * 512
    path = tmp_path / "trainer.nes"
    path.write_bytes(data)

    rom = inspect_rom(path, path.name)

    assert rom.lookups[0][0] == hashlib.sha1(nes[16:]).hexdigest()
    assert rom.crc32s[rom.lookups[0]] == f"{zlib.crc32(nes[16:]):08x}"
    assert rom.crc32s[rom.lookups[1]] == f"{zlib.crc32(data):08x}"
    assert rom.entry["hash"] == hashlib.sha256(data).hexdigest()


def test_nes_trainer_and_padding_are_both_excluded_from_payload_lookup(tmp_path: Path, nes: bytes) -> None:
    data = bytearray(nes)
    data[6] = 4
    data[16:16] = b"T" * 512
    data.extend(bytes(1024))
    path = tmp_path / "trainer.nes"
    path.write_bytes(data)

    rom = inspect_rom(path, path.name)

    assert rom.lookups[0] == (hashlib.sha1(data).hexdigest(), len(data))
    assert rom.lookups[1] == (hashlib.sha1(nes[16:]).hexdigest(), len(nes) - 16)
    assert rom.entry["hash"] == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("tail", [b"D", bytes(4096) + b"D", b"\xff" * 4096])
def test_nonzero_nes_trailing_data_preserves_full_identity_and_payload_fallback(
    tmp_path: Path, nes: bytes, monkeypatch: pytest.MonkeyPatch, tail: bytes,
) -> None:
    path = tmp_path / "extra.nes"
    data = nes + tail
    path.write_bytes(data)
    monkeypatch.setattr("icat.roms.inspection.CHUNK", 1024)
    diagnostics = Diagnostics()

    rom = inspect_rom(path, path.name, diagnostics=diagnostics)

    assert path.read_bytes() == data
    assert rom.entry["hash"] == hashlib.sha256(data).hexdigest()
    assert rom.lookups == [
        (hashlib.sha1(data).hexdigest(), len(data)),
        (hashlib.sha1(nes[16:]).hexdigest(), len(nes) - 16),
    ]
    assert rom.crc32s[rom.lookups[0]] == f"{zlib.crc32(data):08x}"
    assert rom.crc32s[rom.lookups[1]] == f"{zlib.crc32(nes[16:]):08x}"
    event = diagnostics.get_snapshot()["events"][0]
    assert (event["outcome"], event["bytes"], event["nonzero"]) == ("nes_trailing_data", len(tail), True)


@pytest.mark.parametrize("flags", [1, 2, 8])
@pytest.mark.parametrize("tail", [bytes(32), b"B" * 8192])
def test_extended_nes_with_extra_data_uses_only_the_full_lookup(
    tmp_path: Path, nes: bytes, flags: int, tail: bytes,
) -> None:
    data = bytearray(nes)
    data[7] = flags
    path = tmp_path / "extended.nes"
    data.extend(tail)
    path.write_bytes(data)

    rom = inspect_rom(path, path.name)

    assert rom.lookups == [(hashlib.sha1(data).hexdigest(), len(data))]
    assert rom.crc32s[rom.lookups[0]] == f"{zlib.crc32(data):08x}"
    assert rom.entry["hash"] == hashlib.sha256(data).hexdigest()
    assert path.read_bytes() == data


def test_ambiguous_bin_is_not_silently_assigned_a_platform(tmp_path: Path) -> None:
    path = tmp_path / "something.bin"
    path.write_bytes(bytes(512))

    with pytest.raises(CatalogueError, match="Ambiguous"):
        inspect_rom(path, path.name)

    data = bytearray(512)
    data[256:260] = b"SEGA"
    path.write_bytes(data)
    assert inspect_rom(path, path.name).entry["platform"] == "MD"


@pytest.mark.parametrize("kind", ["education", "diamond"])
def test_reported_trailing_nes_shapes_are_preserved_without_repair(tmp_path: Path, kind: str) -> None:
    # Synthetic bytes with the observed sizes/headers; not copies of commercial ROM payloads.

    if kind == "education":
        data = bytes.fromhex("4e45531a100011f10000000000000000") + bytes(262144) + b"\xc0"

    elif kind == "diamond":
        data = bytes.fromhex("4e45531a020101000000000000000000") + bytes(40960) + b"D" + bytes(24575)

    path = tmp_path / "extra.nes"
    path.write_bytes(data)

    rom = inspect_rom(path, path.name)

    assert path.read_bytes() == data
    assert rom.lookups[0] == (hashlib.sha1(data).hexdigest(), len(data))
    assert rom.entry["hash"] == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("data", [b"", bytes(16384), b"NES\x1a" + bytes(12)])
def test_missing_nes_payload_or_header_remains_rejected(tmp_path: Path, data: bytes) -> None:
    path = tmp_path / "broken.nes"
    path.write_bytes(data)

    with pytest.raises(SourceError, match="Invalid iNES header"):
        inspect_rom(path, path.name)

    assert path.read_bytes() == data
