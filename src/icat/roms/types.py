"""Identities and source inventory shared by import and metadata lookup."""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Rom:
    path: Path
    entry: dict
    lookups: list[tuple[str, int]]
    has_thumbnail: bool = False
    crc32s: dict[tuple[str, int], str] = field(default_factory=dict)
    full_lookup: tuple[str, int] | None = None


@dataclass
class Source:
    path: Path
    fingerprint: tuple[int, ...]
    digest: str
    roms: list[Rom]
    header_fixes: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class SourceInventory:
    files: int
    candidates: list[Path]
