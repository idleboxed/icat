"""Shared pinned Libretro DAT storage and identity indexes, independent of ROM import.

SPDX-License-Identifier: BSD-3-Clause
"""

import re
import threading
from _thread import LockType
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from urllib.parse import quote

from .dat import LIMIT, DatNode, read_dat
from .cache import DatasetCache
from ..io.files import read_bytes
from ..io.http import HttpClient


REVISION = "92a7c5adf8c8362d7b88a35042f0211ae3d88316"
BASE = f"https://raw.githubusercontent.com/libretro/libretro-database/{REVISION}/metadat"
NES_SYSTEM = "Nintendo - Nintendo Entertainment System"
MD_SYSTEM = "Sega - Mega Drive - Genesis"


@dataclass
class DatIndex:
    identities: dict[tuple[str, int], set[str]] = field(default_factory=dict)
    owners: dict[str, set[tuple[str, int] | None]] = field(default_factory=dict)
    players: dict[str, set[int | None]] = field(default_factory=dict)
    names: dict[tuple[str, int], set[str | None]] = field(default_factory=dict)
    counterparts: dict[tuple[str, int], set[tuple[str, int]]] = field(default_factory=dict)


def read_checksum(node: DatNode, name: str, length: int) -> str | None:
    value = node.get_scalar(name)
    return value.lower() if value and re.fullmatch(rf"[0-9A-Fa-f]{{{length}}}", value) else None


@dataclass
class DatCache:
    lock: LockType = field(default_factory=threading.Lock)
    tables: dict[tuple[Path, str | None, str | None], DatIndex] = field(default_factory=dict)


class LibretroDatDatabase:
    hosts = frozenset({"raw.githubusercontent.com"})
    # Reuse already downloaded databases; changing consumers must not invalidate the PC cache.
    dataset_namespace = "libretro-players"

    def __init__(
        self, http: HttpClient, datasets: DatasetCache, *, dat_cache: DatCache | None = None
    ) -> None:
        self.http, self.datasets = http, datasets
        shared = dat_cache if dat_cache is not None else DatCache()
        self.lock, self.tables = shared.lock, shared.tables

    def validate_dataset(self, path: Path, *, system: str | None = None, kind: str | None = None) -> None:
        identity = (path, system, kind)

        if identity in self.tables:
            return

        records = read_dat(read_bytes(path, LIMIT))
        tag, header = next(records, (None, None))

        if tag != "clrmamepro" or not header.get_scalar("name") or (system and header.get_scalar("name") != system):
            raise ValueError("Unexpected DAT platform/header")

        index = DatIndex()
        pairs: dict[tuple[str, str], dict[str, set[tuple[str, int] | None]]] = {}
        games = 0

        for tag, game in records:

            if tag != "game":
                raise ValueError("Unexpected DAT record")

            games += 1
            roms = game.get_children("rom")

            for rom in roms:
                crc = read_checksum(rom, "crc", 8)

                if crc is None:
                    continue

                if kind == "no-intro":
                    sha1, size = read_checksum(rom, "sha1", 40), rom.get_scalar("size")
                    rom_id = (
                        (sha1, int(size))
                        if sha1 and size and re.fullmatch(r"[0-9]{1,12}", size) and int(size) > 0 and len(roms) == 1
                        else None
                    )
                    index.owners.setdefault(crc, set()).add(rom_id)

                    if rom_id:
                        index.identities.setdefault(rom_id, set()).add(crc)
                        index.names.setdefault(rom_id, set()).add(game.get_scalar("name"))

                    # NES DAT has separate .unh and .nes records for the same canonical
                    # release. Join those records, never the user's filename or a CRC alone.
                    filename = rom.get_scalar("name") or ""
                    name = game.get_scalar("name")
                    suffix = Path(filename).suffix

                    if system == NES_SYSTEM and name and suffix in {".unh", ".nes"}:
                        pair = pairs.setdefault((name, filename[:-4]), {})
                        pair.setdefault(suffix, set()).add(rom_id)

                elif kind == "maxusers":
                    users = game.get_scalar("users")
                    count = (
                        int(users)
                        if users and re.fullmatch(r"[0-9]{1,3}", users) and 1 <= int(users) <= 255 and len(roms) == 1
                        else None
                    )
                    index.players.setdefault(crc, set()).add(count)

        if (
            not games
            or (kind == "no-intro" and not index.identities)
            or (kind == "maxusers" and not any(values - {None} for values in index.players.values()))
        ):
            raise ValueError("Empty or incompatible DAT")

        for pair in pairs.values():
            raw, headered = pair.get(".unh", set()), pair.get(".nes", set())

            if len(raw) != 1 or len(headered) != 1 or None in raw or None in headered:
                continue

            payload_id, full_id = next(iter(raw)), next(iter(headered))

            if full_id[1] == payload_id[1] + 16:
                index.counterparts.setdefault(payload_id, set()).add(full_id)

        self.tables[identity] = index

    def get_table(self, platform: str, kind: str, *, system: str) -> tuple[DatIndex | None, str]:
        url = f"{BASE}/{kind}/{quote(system)}.dat"
        validate = partial(self.validate_dataset, system=system, kind=kind)

        def download() -> bytes | None:
            return self.http.get(url, hosts=self.hosts, limit=LIMIT, redirects=True, cache=False)

        def prepare(data: bytes) -> bytes:
            return data

        path = self.datasets.obtain(
            f"{self.dataset_namespace}-{REVISION[:12]}-{platform.lower()}-{kind}", url,
            download=download, prepare=prepare, validate=validate, limit=LIMIT,
        )

        if path is None:
            return None, url

        validate(path)
        return self.tables[path, system, kind], url
