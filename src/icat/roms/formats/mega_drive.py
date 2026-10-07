"""Identify headerless Mega Drive BINs without guessing or modifying their bytes.

SPDX-License-Identifier: BSD-3-Clause
"""

from urllib.parse import quote

from ...databases.libretro import BASE, MD_SYSTEM, LibretroDatDatabase


class MegaDriveIdentifier:
    url = f"{BASE}/no-intro/{quote(MD_SYSTEM)}.dat"

    def __init__(self, database: LibretroDatDatabase) -> None:
        self.database = database

    def is_known_rom(self, *, sha1: str, size: int, crc32: str) -> bool:
        """Require the full-file SHA-1, size and unambiguous CRC in the MD table."""

        with self.database.lock:
            index, _url = self.database.get_table("MD", "no-intro", system=MD_SYSTEM)

        identity = (sha1, size)
        return (
            index is not None
            and index.identities.get(identity) == {crc32}
            and index.owners.get(crc32) == {identity}
        )
