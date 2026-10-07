"""Verified Libretro metadata identities backed by the shared DAT storage.

SPDX-License-Identifier: BSD-3-Clause
"""

import logging
from pathlib import Path

from ...catalogue.codec import is_clean_text
from ...databases.cache import DatasetCache
from ...databases.libretro import DatCache, DatIndex, LibretroDatDatabase
from ...io.http import HttpClient
from .base import FileMetadataSource
from ..types import LookupContext
from .platforms import LIBRETRO
from ...io import paths


logger = logging.getLogger(__name__)
# NDS has no maxusers DAT; CD containers are not No-Intro cartridge identities.
SYSTEMS = {key: value for key, value in LIBRETRO.items() if key not in {"NDS", "PSX", "SCD"}}


class LibretroDatSource(FileMetadataSource):
    hosts = LibretroDatDatabase.hosts
    dataset_namespace = LibretroDatDatabase.dataset_namespace

    def __init__(
        self, http: HttpClient, datasets: DatasetCache, *, key: str | None = None, dat_cache: DatCache | None = None
    ) -> None:
        super().__init__(http, datasets, key=key)
        self.database = LibretroDatDatabase(http, datasets, dat_cache=dat_cache)
        self.lock, self.tables = self.database.lock, self.database.tables

    def validate_dataset(self, path: Path, *, system: str | None = None, kind: str | None = None) -> None:
        self.database.validate_dataset(path, system=system, kind=kind)

    def get_table(self, platform: str, kind: str) -> tuple[DatIndex | None, str]:
        return self.database.get_table(platform, kind, system=SYSTEMS[platform])

    def find_matching_identities(self, context: LookupContext, index: DatIndex) -> list[tuple[str, int]] | None:
        matched = []

        for rom_id in context.rom.lookups:
            crcs = index.identities.get(rom_id)

            if not crcs:
                continue

            crc = context.rom.crc32s.get(rom_id)

            if crcs != {crc} or index.owners.get(crc) != {rom_id}:
                logger.warning(
                    "%s — ambiguous Libretro ROM/CRC; ignored",
                    paths.quote_path(Path(context.rom.entry["image"]).name),
                )
                self.http.diagnostics.emit("issue", "ambiguous_rom_identity")
                return None

            matched.append(rom_id)

        return matched

    def find_canonical_name(self, context: LookupContext) -> tuple[str | None, str | None]:
        platform = context.rom.entry["platform"]

        if platform not in SYSTEMS:
            return None, None

        with self.lock:
            index, url = self.get_table(platform, "no-intro")

        if index is None:
            return None, None

        identities = self.find_matching_identities(context, index)

        if identities is None:
            raise ValueError("Ambiguous Libretro identity")

        names = {name for rom_id in identities for name in index.names.get(rom_id, {None})}

        if len(names) > 1:
            self.http.diagnostics.emit("issue", "ambiguous_canonical_name")
            raise ValueError("Conflicting Libretro canonical names")

        name = next(iter(names), None)

        if not is_clean_text(name) or not name.strip():
            return None, None

        return name, url
