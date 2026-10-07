"""Synthetic cartridge fixtures only; no real ROMs or network."""

import base64
import hashlib
import struct
import json
import sqlite3
import zipfile
from collections.abc import Callable, Sequence
from io import BytesIO
from ctypes import CDLL
from pathlib import Path
from typing import Any
from urllib.parse import quote

import py7zr
import pytest
from PIL import Image

from icat.roms.formats.chd_reader import FRAME_BYTES, load_library
from icat.errors import CatalogueError
from icat.metadata.types import Metadata
from icat.metadata.sources.openvgdb import OpenVgdbSource
from icat.roms.types import Rom
from icat.roms.inspection import inspect_rom
from icat.databases.libretro import BASE
from icat.databases.cache import DatasetCache
from icat.io.http import HttpClient
from icat.metadata.sources.libretro_dat import SYSTEMS
from icat.metadata.sources.platforms import LIBRETRO
from icat.operations.options import Options


@pytest.fixture(autouse=True)
def logs_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("icat.io.paths.LOGS_DIR", tmp_path / "logs")


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def reject_unexpected_call(*args: object, **kwargs: object) -> None:
        raise AssertionError("Tests must not use the network")

    monkeypatch.setattr("icat.io.http.DEFAULT_REQUEST_INTERVALS", {})
    monkeypatch.setattr("icat.io.http.DEFAULT_RETRIES", 0)
    monkeypatch.setattr("socket.socket.connect", reject_unexpected_call)
    monkeypatch.setattr("socket.socket.connect_ex", reject_unexpected_call)
    monkeypatch.setattr("socket.getaddrinfo", reject_unexpected_call)


@pytest.fixture
def nes() -> bytes:
    return b"NES\x1a\x01\x01" + bytes(10) + b"P" * 16384 + b"C" * 8192


@pytest.fixture
def sevenzip_archive() -> Callable[..., Path]:
    """Small synthetic archives; low compression keeps the test matrix fast."""

    def make(path: Path, entries: Sequence[tuple[str, bytes]], **kwargs: Any) -> Path:
        kwargs.setdefault("filters", [{"id": py7zr.FILTER_LZMA2, "preset": 0}])

        with py7zr.SevenZipFile(path, "w", **kwargs) as archive:
            archive.set_encoded_header_mode(False)

            for name, data in entries:
                archive.writestr(data, name)

        return path

    return make


@pytest.fixture
def options(tmp_path: Path) -> Options:
    src = tmp_path / "rom"
    src.mkdir()
    return Options(src, tmp_path / "games", tmp_path / "cache", tmp_path / "logs")


@pytest.fixture
def provider() -> Any:
    class Offline:
        def lookup(self, rom: Rom) -> Metadata:
            return Metadata()

    return Offline()


@pytest.fixture
def png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (64, 48), "red").save(output, "PNG")
    return output.getvalue()


@pytest.fixture
def lookup_response(nes: bytes) -> dict:
    return {
        "name": "Synthetic Game",
        "platform": {"metadata": [{"objectType": "Platform", "id": "18", "status": "Mapped", "source": "IGDB"}]},
        "signature": {
            "rom": {"sha1": hashlib.sha1(nes[16:]).hexdigest(), "size": len(nes) - 16, "country": {"JP": "Japan"}},
            "game": {"year": "1991-02-03", "description": "Synthetic description."},
        },
        "metadata": [{"objectType": "Game", "id": "123", "status": "Mapped", "source": "IGDB"}],
    }


@pytest.fixture
def rom(tmp_path: Path, nes: bytes) -> Rom:
    path = tmp_path / "Game (Japan).nes"
    path.write_bytes(nes)
    return inspect_rom(path, path.name)


@pytest.fixture
def lookup_rule(lookup_response: dict) -> str:
    digest = lookup_response["signature"]["rom"]["sha1"]
    return f"GET https://hasheous.org/api/v1/Lookup/ByHash/sha1/{digest} -> 200 :{json.dumps(lookup_response)}"


@pytest.fixture
def openvgdb_archive(nes: bytes) -> Callable[..., bytes]:
    """Tiny synthetic SQLite with only the actual columns used by the source."""

    def make(
        *, digest: str | None = None, size: int | None = None, platform: int = 25, region: int = 13,
        releases: Sequence[tuple] | None = None,
    ) -> bytes:
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript("""
                CREATE TABLE ROMs (romID INTEGER, systemID INTEGER, regionID INTEGER,
                    romHashSHA1 TEXT, romSize INTEGER);
                CREATE TABLE REGIONS (regionID INTEGER, regionName TEXT);
                CREATE TABLE RELEASES (romID INTEGER, regionLocalizedID INTEGER, releaseTitleName TEXT,
                    releaseDescription TEXT, releaseGenre TEXT, releaseDate TEXT);
                INSERT INTO REGIONS VALUES (13, 'Japan'), (21, 'USA'), (22, 'World');
            """)
            connection.execute(
                "INSERT INTO ROMs VALUES (1,?,?,?,?)",
                (
                    platform,
                    region,
                    digest or hashlib.sha1(nes[16:]).hexdigest().upper(),
                    len(nes) - 16 if size is None else size,
                ),
            )
            releases = (
                releases
                if releases is not None
                else [
                    (
                        13,
                        "Synthetic Game",
                        "<p>Explore &amp; rescue.</p><p>Second paragraph.</p>",
                        "Action,Platformer,2D",
                        "Sep 9, 1991",
                    )
                ]
            )
            connection.executemany("INSERT INTO RELEASES VALUES (1,?,?,?,?,?)", releases)
            stream = BytesIO()

            with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("openvgdb.sqlite", connection.serialize())

            return stream.getvalue()

        finally:
            connection.close()

    return make


@pytest.fixture
def canonical_dat_rule(rom: Rom) -> str:
    sha1, size = rom.lookups[0]
    crc = rom.crc32s[rom.lookups[0]]
    content = (
        f"clrmamepro ( name \"{SYSTEMS["NES"]}\" )\n"
        f"game ( name \"Game (Japan)\" rom ( sha1 {sha1} size {size} crc {crc} ) )\n"
    )
    return f"GET {BASE}/no-intro/{quote(SYSTEMS["NES"])}.dat -> 200 :{content}"


@pytest.fixture
def thumbnail_index_rules() -> Callable[..., list[str]]:
    def make(platform: str = "NES", names: Sequence[str] = ("Game (Japan).png",)) -> list[str]:
        repository = LIBRETRO[platform].replace(" ", "_")
        base = f"https://api.github.com/repos/libretro-thumbnails/{repository}/git/trees/"
        root = {"truncated": False, "tree": [{"path": "Named_Snaps", "type": "tree", "sha": "a" * 40}]}
        tree = {"truncated": False, "tree": [{"path": name, "type": "blob"} for name in names]}
        return [f"GET {base}master -> 200 :{json.dumps(root)}", f"GET {base}{"a" * 40} -> 200 :{json.dumps(tree)}"]

    return make


@pytest.fixture
def native_chd() -> CDLL:
    try:
        return load_library()

    except CatalogueError as exc:
        pytest.skip(f"{exc}")

@pytest.fixture
def chd_image() -> Callable[..., bytes]:
    """Minimal uncompressed CD CHDs; parameters describe deliberately synthetic layouts."""

    def make(
        platform: str = "PSX", *, mode: str = "MODE2_RAW", pregap: int = 0, stored: bool = False,
        metadata: bytes | None = None, version: int = 5,
    ) -> bytes:
        start = pregap if stored else 0
        frames = ((32 + start + 7) // 8) * 8
        hunk_bytes = 8 * FRAME_BYTES
        payload = bytearray(frames * FRAME_BYTES)
        offset = {"MODE1": 0, "MODE1_RAW": 16, "MODE2": 8, "MODE2_FORM1": 0, "MODE2_RAW": 24}[mode]

        for number in range(start, frames):

            if mode.endswith("_RAW"):
                pos = number * FRAME_BYTES
                payload[pos:pos + 12] = b"\0" + b"\xff" * 10 + b"\0"
                payload[pos + 15] = 1 if mode == "MODE1_RAW" else 2

        if platform in ("SCD", "both"):
            pos = start * FRAME_BYTES + offset
            payload[pos:pos + 14] = b"SEGADISCSYSTEM"

        if platform in ("PSX", "both"):
            pos = (start + 16) * FRAME_BYTES + offset
            payload[pos:pos + 7] = b"\x01CD001\x01"
            payload[pos + 8:pos + 40] = b"PLAYSTATION".ljust(32, b" ")

        if platform in ("PSX", "both"):
            def record(lba: int, size: int, name: bytes, flags: int = 0) -> bytearray:
                data = bytearray(33 + len(name) + (not len(name) % 2))
                data[0] = len(data)
                struct.pack_into("<I", data, 2, lba)
                struct.pack_into(">I", data, 6, lba)
                struct.pack_into("<I", data, 10, size)
                struct.pack_into(">I", data, 14, size)
                data[25], data[32] = flags, len(name)
                data[28:32] = b"\x01\0\0\x01"
                data[33:33 + len(name)] = name
                return data

            pvd = (start + 16) * FRAME_BYTES + offset
            root = record(20, 2048, b"\0", 2)
            payload[pvd + 128:pvd + 132] = b"\0\x08\x08\0"
            payload[pvd + 156:pvd + 156 + len(root)] = root
            config = b"BOOT = cdrom:\\PSX.EXE;1\r\n"
            entry = record(24, len(config), b"SYSTEM.CNF;1")
            pos = (start + 20) * FRAME_BYTES + offset
            payload[pos:pos + len(entry)] = entry
            pos = (start + 24) * FRAME_BYTES + offset
            payload[pos:pos + len(config)] = config

        if metadata is None:
            metadata = (
                f"TRACK:1 TYPE:{mode} SUBTYPE:NONE FRAMES:{frames} "
                f"PREGAP:{pregap} PGTYPE:{"V" if stored else ""}{mode} PGSUB:NONE POSTGAP:0"
            ).encode()

        header_length = {3: 120, 4: 108, 5: 124}[version]
        header = bytearray(header_length)
        header[:8] = b"MComprHD"
        struct.pack_into(">II", header, 8, header_length, version)
        hunks = len(payload) // hunk_bytes
        meta_offset = header_length + (hunks * 4 if version == 5 else (hunks + 1) * 16)
        metadata_record = b"CHT2" + struct.pack(">IQ", len(metadata) + 1, 0) + metadata + b"\0"
        data_offset = ((meta_offset + len(metadata_record) + hunk_bytes - 1) // hunk_bytes) * hunk_bytes

        if version == 5:
            struct.pack_into(">QQQII", header, 32, len(payload), header_length, meta_offset, hunk_bytes, FRAME_BYTES)
            table = b"".join(struct.pack(">I", data_offset // hunk_bytes + hunk) for hunk in range(hunks))

        else:
            struct.pack_into(">IQQ", header, 24, hunks, len(payload), meta_offset)
            struct.pack_into(">I", header, 76 if version == 3 else 44, hunk_bytes)
            table = b"".join(
                struct.pack(">QI", data_offset + hunk * hunk_bytes, 0)
                + struct.pack(">HB", hunk_bytes & 0xffff, hunk_bytes >> 16) + b"\x12"
                for hunk in range(hunks)
            ) + b"EndOfListCookie\0"

        data = header + table + metadata_record
        return bytes(data + bytes(data_offset - len(data)) + payload)

    return make

@pytest.fixture
def compressed_chd() -> dict[str, bytes]:
    # 32 synthetic raw sectors; sync/mode fields, platform markers and a minimal PS1 boot directory.
    # Encoded once with MAME chdman 0.264: createcd -i sample.cue -o sample.chd -c cdlz -np 1.
    # These two sub-1-KiB files exercise real LZMA decoding, not a mock/native-code replacement.
    samples = {
        "PSX": (
            "TUNvbXBySEQAAAB8AAAABWNkbHoAAAAAAAAAAAAAAAAAAAAAAAEyAAAAAAAAAAJOAAAAAAAAAHwAAEyAAAAJkO22HMxIA93a"
            "5X+kt2ZKoLTCk2MOp52Lk6XVcT12GXr080aCI4Mq7cIAAAAAAAAAAAAAAAAAAAAAAAAAAENIVDIBAABZAAAAAAAAAABUUkFD"
            "SzoxIFRZUEU6TU9ERTJfUkFXIFNVQlRZUEU6Tk9ORSBGUkFNRVM6MzIgUFJFR0FQOjAgUEdUWVBFOk1PREUxIFBHU1VCOk5P"
            "TkUgUE9TVEdBUDowAP8AQgAAbP6MEOdNxuhpFQyE9HnQVTJsaEE6deCPFrzY8sOuR5czFacZEg7WdHqT1g1GY6Gd8fM69efB"
            "a5mdFNbU6JMAAGNgGAWjYOQCAO4AnQAAP/USGVAY/z4QgWh7Q6QfC6ymWrkvDf7HU+Wq4q4Z7K7uxFP3AcPmw1TbJC+ev8Yu"
            "8x1k868SuHsn1w+SdjByZNfX7+72fNZjaCzvtr473aO3ElT5aAOHD1jL3q4rE+AUWUtIcUIwnspZZPlMipOwn0Ezp2Ld3lOc"
            "C1DJ1KbYQJ3Ss8VIjUdg+GiSAwcOtbu+/JB6EbL46esx0QBjYBgFo2DkAgD+AGYAAD/1EhlQGP8/ei8tOOgAUVdq5SJYUPFX"
            "Xom6c8hYI6ac2m5VVIYYioU/TzyznOlhDIs1cpnbOTHYqejDKIktw6svrOvvxjAZC65ZO1B3Dp4+MDqaaLNTOg4vkNPTHHJD"
            "Qef5oABjYBgFo2DkAgAAAAAPAAAAAADlyuwIAAAAERBREQNE4kfakxfHLu2Q"
        ),
        "SCD": (
            "TUNvbXBySEQAAAB8AAAABWNkbHoAAAAAAAAAAAAAAAAAAAAAAAEyAAAAAAAAAAGXAAAAAAAAAHwAAEyAAAAJkEjeCXmZ2NUY"
            "XmYEr9J7lllqpX+dV+dxqLvR204TKww4nxvhzz74X2kAAAAAAAAAAAAAAAAAAAAAAAAAAENIVDIBAABZAAAAAAAAAABUUkFD"
            "SzoxIFRZUEU6TU9ERTFfUkFXIFNVQlRZUEU6Tk9ORSBGUkFNRVM6MzIgUFJFR0FQOjAgUEdUWVBFOk1PREUxIFBHU1VCOk5P"
            "TkUgUE9TVEdBUDowAAAAVQAAP/USGVALHcC4MpqPwfmeMaMDSlfz6KduqKC4XKF8QlTCZWfRqTvOPyUi/we68WvKcuqcQ50r"
            "+Rs2ypuOaUx342yfVC70MnEd1BzPbhuOEL7MAQBjYBgFo2DkAgAAAEUAAD/1EhlQCIQny1uAN0j92g9AUtkULvQTOXd5wxPc"
            "kXVKcLi3yUTcoFvbxY6GWPlBGbOpTLFXuru+0Nc3M/TeZGk8oQBjYBgFo2DkAgAAAAAMAAAAAADloPYHAAAAERBSIQLTC9VN"
            "H9rg"
        ),
    }
    return {platform: base64.b64decode(data) for platform, data in samples.items()}

@pytest.fixture
def source(tmp_path: Path) -> OpenVgdbSource:
    return OpenVgdbSource(HttpClient(tmp_path / "cache"), DatasetCache(tmp_path / "datasets"))
