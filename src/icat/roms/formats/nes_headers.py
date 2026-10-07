"""Conservative iNES size repair, using a pinned hash/board database.

SPDX-License-Identifier: BSD-3-Clause
"""

import hashlib
import os
import re
import stat
import zlib
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from ...errors import CatalogueError
from ...databases.cache import DatasetCache
from ...io.files import CHUNK, read_bytes, open_regular_file, get_file_stamp
from ...io.http import HttpClient


REVISION = "224713b257c8620513c1c9ca28dc7f8dea711e18"
URL = f"https://raw.githubusercontent.com/libretro/nestopia/{REVISION}/NstDatabase.xml"
DATASET = f"nes-headers-{REVISION[:12]}"
LIMIT = 2 * 1024 * 1024


@dataclass(frozen=True)
class Board:
    crc32: str
    prg: int
    chr: int
    mapper: int
    battery: bool


def parse_rom_size(value: str | None) -> int:

    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,5}k", value):
        raise ValueError("Invalid NES database ROM size")

    return int(value[:-1]) * 1024


class NesHeaderFixer:
    def __init__(self, http: HttpClient, datasets: DatasetCache) -> None:
        self.http, self.datasets = http, datasets
        self.tables: dict[Path, dict[str, Board | None]] = {}

    def validate_dataset(self, path: Path) -> None:

        if path in self.tables:
            return

        raw = read_bytes(path, LIMIT).decode("utf-8")
        # DTDs/entities are unnecessary for this pinned data format.

        if "<!DOCTYPE" in raw.upper() or "<!ENTITY" in raw.upper():
            raise ValueError("Unexpected XML declarations in NES database")

        try:
            root = ElementTree.fromstring(raw)

        except ElementTree.ParseError:
            raise ValueError("Invalid NES database XML") from None

        if root.tag != "database" or root.get("version") != "1.0":
            raise ValueError("Unexpected NES database format")

        table: dict[str, Board | None] = {}

        for cart in root.iter("cartridge"):
            digest = cart.get("sha1", "").lower()

            if not re.fullmatch(r"[0-9a-f]{40}", digest):
                continue

            board = None

            try:
                nodes = cart.findall("board")

                if len(nodes) != 1 or cart.get("system") not in {"Famicom", "NES-NTSC", "NES-PAL"}:
                    raise ValueError("Unsupported or ambiguous NES board")

                node = nodes[0]
                prg, chr_rom = node.findall("prg"), node.findall("chr")
                crc, mapper = cart.get("crc", "").lower(), node.get("mapper", "")

                if (
                    len(prg) != 1 or len(chr_rom) != 1 or not re.fullmatch(r"[0-9a-f]{8}", crc)
                    or not re.fullmatch(r"[0-9]{1,3}", mapper) or int(mapper) > 255
                ):
                    raise ValueError("Incomplete NES board identity")

                prg_size, chr_size = parse_rom_size(prg[0].get("size")), parse_rom_size(chr_rom[0].get("size"))

                if not (0 < prg_size <= 255 * 16384 and prg_size % 16384 == 0):
                    raise ValueError("Unsupported NES PRG size")

                if not (0 < chr_size <= 255 * 8192 and chr_size % 8192 == 0):
                    raise ValueError("Unsupported NES CHR size")

                battery = any(ram.get("battery") == "1" for ram in node.findall("wram"))
                board = Board(crc, prg_size, chr_size, int(mapper), battery)

            except ValueError:
                pass

            # An incomplete/conflicting duplicate must not be hidden by a good row.
            table[digest] = board if digest not in table or table[digest] == board else None

        if not any(value is not None for value in table.values()):
            raise ValueError("Empty or incompatible NES database")

        self.tables[path] = table

    def get_database(self) -> tuple[Path | None, dict[str, Board | None]]:
        def download() -> bytes | None:
            return self.http.get(URL, hosts=frozenset({"raw.githubusercontent.com"}), limit=LIMIT, cache=False)

        def prepare(data: bytes) -> bytes:
            return data

        path = self.datasets.obtain(
            DATASET, URL,
            download=download, prepare=prepare, validate=self.validate_dataset, limit=LIMIT,
        )
        return path, self.tables[path] if path is not None else {}

    def repair_staged(self, path: Path, *, name: str) -> dict | None:
        """Repair only a private staging copy, never a source or indexed destination."""

        if Path(name).suffix.lower() != ".nes":
            return None

        before = get_file_stamp(path)

        with open_regular_file(path) as stream:
            header = stream.read(16)

            if (
                len(header) != 16 or header[:4] != b"NES\x1a" or header[7] & 15
                or header[6] & 4 or any(header[8:]) or not header[4]
            ):
                return None

            # Do not rewrite valid files or trim trailing data/overdumps. Extended
            # consoles, trainers, NES 2.0 and nonzero reserved fields stay strict.
            expected = 16 + header[4] * 16384 + header[5] * 8192

            if before[2] >= expected:
                return None

            digest, crc, size = hashlib.sha1(), 0, 0

            while chunk := stream.read(CHUNK):
                digest.update(chunk)
                crc, size = zlib.crc32(chunk, crc), size + len(chunk)

        database_path, table = self.get_database()
        board = table.get(digest.hexdigest())

        if board is None or (
            board.crc32 != f"{crc:08x}" or board.prg + board.chr != size
            or board.prg != header[4] * 16384 or board.mapper != (header[6] >> 4 | header[7] & 240)
        ):
            return None

        updated = bytearray(header)
        updated[5] = board.chr // 8192

        if board.battery:
            updated[6] |= 2

        if get_file_stamp(path) != before:
            raise CatalogueError("Staged ROM changed before header repair", path=path)

        # The staging tree is private. Still reject symlink/hardlink replacement,
        # and compare the complete read identity before writing these 16 bytes.
        fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))

        try:
            info = os.fstat(fd)

            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (info.st_dev, info.st_ino) != before[:2]:
                raise CatalogueError("Staged ROM changed before header repair", path=path)

            with os.fdopen(fd, "r+b", closefd=False) as stream:

                if stream.read(16) != header:
                    raise CatalogueError("Staged NES header changed", path=path)

                original, corrected = hashlib.sha256(header), hashlib.sha256(updated)
                verified, verified_size = hashlib.sha1(), 0

                while chunk := stream.read(CHUNK):
                    original.update(chunk)
                    corrected.update(chunk)
                    verified.update(chunk)
                    verified_size += len(chunk)

                if verified.digest() != digest.digest() or verified_size != size:
                    raise CatalogueError("Staged NES payload changed", path=path)

                stream.seek(0)

                if stream.write(updated) != 16:
                    raise OSError("Incomplete NES header write")

                stream.flush()
                os.fsync(stream.fileno())

        finally:
            os.close(fd)

        return {
            "name": name, "original_header": header.hex(), "header": updated.hex(),
            "original_sha256": original.hexdigest(), "sha256": corrected.hexdigest(),
            "payload_sha1": digest.hexdigest(), "payload_size": size, "payload_crc32": f"{crc:08x}",
            "chr_bytes_before": header[5] * 8192, "chr_bytes_after": board.chr,
            "battery_added": not bool(header[6] & 2) and board.battery,
            "database_url": URL, "database_sha256": database_path.name,
        }
