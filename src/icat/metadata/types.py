"""Metadata results and lookup state independent of concrete sources."""

from dataclasses import dataclass, field
from pathlib import Path

from ..roms.types import Rom
from .genres import normalize_genre


FIELDS = frozenset({"title", "region", "year", "players", "genre", "description", "thumbnail"})


@dataclass
class Metadata:
    fields: dict = field(default_factory=dict)
    thumbnail: bytes | None = None
    sources: list[str] = field(default_factory=list)
    identifiers: dict[str, str] = field(default_factory=dict)
    field_sources: dict[str, str] = field(default_factory=dict)
    outcome: str = "no_match"


@dataclass
class LookupContext:
    rom: Rom
    values: dict
    identifiers: dict[str, str] = field(default_factory=dict)
    has_thumbnail: bool = False
    resolved: set[str] = field(default_factory=set)

    @property
    def missing(self) -> frozenset[str]:
        missing = {key for key in FIELDS - {"thumbnail"} if self.values.get(key) is None}

        if normalize_genre(self.values.get("genre")) == "Other":
            missing.add("genre")

        if "title" not in self.resolved and self.values.get("title") == Path(self.rom.entry["image"]).stem:
            missing.add("title")

        if not self.has_thumbnail:
            missing.add("thumbnail")

        return frozenset(missing)
