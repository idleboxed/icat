"""Persistent OpenVGDB SQLite release; exact SHA-1/size/platform matching, no name search."""

import zipfile
from io import BytesIO
from pathlib import Path

from ...databases.cache import read_database
from ..genres import normalize_genre, select_genre
from .base import FileMetadataSource
from ..types import LookupContext, Metadata


SYSTEMS = {
    "NES": 25,
    "FDS": 18,
    "GB": 19,
    "GBC": 21,
    "GBA": 20,
    "MD": 33,
    "32X": 29,
    "SMS": 31,
    "GG": 30,
    "SG1000": 35,
    "SNES": 26,
    "PCE": 14,
    "WS": 9,
    "WSC": 10,
    "A2600": 3,
    "A5200": 4,
    "A7800": 5,
    "N64": 23,
    "NDS": 24,
}
URL = "https://github.com/OpenVGDB/OpenVGDB/releases/download/v29.0/openvgdb.zip"
LIMIT = 100 * 1024 * 1024
QUERY = """
SELECT R.romID, R.regionID, L.regionLocalizedID, L.releaseTitleName,
       L.releaseDescription, L.releaseGenre, L.releaseDate, G.regionName
FROM ROMs R JOIN RELEASES L ON L.romID = R.romID
LEFT JOIN REGIONS G ON G.regionID = R.regionID
WHERE R.romHashSHA1 IN (?, ?) AND R.romSize = ? AND R.systemID = ?
"""


class OpenVgdbSource(FileMetadataSource):
    name = "openvgdb"
    provides = frozenset({"title", "description", "genre", "year", "region"})
    hosts = frozenset({"github.com", "release-assets.githubusercontent.com"})

    def unpack(self, data: bytes) -> bytes:
        try:

            with zipfile.ZipFile(BytesIO(data)) as archive:
                members = [member for member in archive.infolist() if member.filename == "openvgdb.sqlite"]

                if len(members) != 1 or members[0].file_size > LIMIT or members[0].flag_bits & 1:
                    raise ValueError("Invalid OpenVGDB archive member")
                # Never extract archive paths; resource-fork/extra entries are ignored.

                with archive.open(members[0]) as stream:
                    content = stream.read(LIMIT + 1)

                if len(content) > LIMIT:
                    raise ValueError("OpenVGDB exceeds size limit")

                return content

        except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
            raise ValueError("Invalid OpenVGDB archive") from exc

    def validate_dataset(self, path: Path) -> None:

        with read_database(path) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}

            if not {"ROMs", "RELEASES", "REGIONS"}.issubset(tables):
                raise ValueError("Unexpected OpenVGDB schema")

            db.execute(QUERY + " LIMIT 0", ("", "", 0, 0))

            if db.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
                raise ValueError("Corrupt OpenVGDB database")

    def fetch(self, context: LookupContext) -> Metadata:
        platform = SYSTEMS.get(context.rom.entry["platform"])

        if platform is None:
            return Metadata()

        path = self.obtain_dataset("v29", URL, download_limit=16 * 1024 * 1024, limit=LIMIT)

        if path is None:
            return Metadata()

        with read_database(path) as db:
            rows = []

            for digest, size in context.rom.lookups:
                rows = db.execute(QUERY, (digest.lower(), digest.upper(), size, platform)).fetchmany(101)

                if rows:
                    break

        if not rows or len(rows) > 100:
            return Metadata()

        # Localized release rows may describe different regions. Prefer the ROM's exact region;
        # otherwise use only values shared by all candidates, never an arbitrary first release.
        regional = [row for row in rows if row["regionLocalizedID"] == row["regionID"]]
        descriptive = regional or rows
        genres = [self.parse_genre(row["releaseGenre"]) for row in descriptive]
        specific = [genre for genre in genres if normalize_genre(genre) not in (None, "Other")]
        fields = {
            "title": self.select_consensus_value(
                [self.normalize_text(row["releaseTitleName"]) for row in descriptive]
            ),
            "description": self.select_consensus_value(
                [self.normalize_text(row["releaseDescription"], html=True, multiline=True) for row in descriptive]
            ),
            "genre": self.select_consensus_value(specific or genres),
            # Do not apply an unrelated localized release's date to this ROM.
            "year": self.select_consensus_value([self.parse_year(row["releaseDate"]) for row in regional]),
        }
        region = self.select_consensus_value([row["regionName"] for row in rows])

        if region == "World":
            fields["region"] = []

        elif isinstance(region, str) and region != "Unknown":
            fields["region"] = [region_name.strip() for region_name in region.split(",")]

        identifiers = {"title": fields["title"]} if fields["title"] else {}
        return Metadata(fields=fields, sources=[URL], identifiers=identifiers)

    @staticmethod
    def parse_genre(value: object) -> str | None:

        if not isinstance(value, str):
            return None

        parts = [genre for part in value.split(",") if (genre := part.strip())]
        # Arcade is a standalone genre, but a qualifier in e.g. Driving,Racing,Arcade.

        if any(normalize_genre(genre) not in (None, "Other", "Arcade") for genre in parts):
            parts = [genre for genre in parts if normalize_genre(genre) != "Arcade"]

        # Pass unknowns to the common validator so they are visible in diagnostics.
        return select_genre(reversed(parts))
