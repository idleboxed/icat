"""Shared state for explicit phases; resources are acquired by the session runner."""

from dataclasses import dataclass, field
from pathlib import Path

from ..catalogue.preferences import Preferences
from ..metadata.provider import Provider
from ..metadata.types import Metadata
from .options import Options
from ..reporting.diagnostics import Diagnostics
from ..reporting.journal import RunJournal
from ..reporting.progress import RunProgress
from ..roms.formats.mega_drive import MegaDriveIdentifier
from ..roms.formats.nes_headers import NesHeaderFixer
from ..roms.types import Rom, Source, SourceInventory


@dataclass
class Session:
    options: Options
    src: Path | None
    dst: Path
    staging: Path | None
    provider: Provider
    diagnostics: Diagnostics
    progress: RunProgress
    operation: RunJournal
    catalogue_path: Path
    original: bytes | None
    entries: list[dict]
    inventory: SourceInventory
    preferences: Preferences | None
    header_fixer: NesHeaderFixer | None
    md_identifier: MegaDriveIdentifier | None
    trash_entries: list[dict] = field(default_factory=list)
    by_hash: dict[str, dict] = field(default_factory=dict)
    existing_hashes: set[str] = field(default_factory=set)
    sources: list[Source] = field(default_factory=list)
    unique: dict[str, Rom] = field(default_factory=dict)
    duplicate_paths: set[str] = field(default_factory=set)
    metadata: dict[str, Metadata] = field(default_factory=dict)
    ordered: list[dict] = field(default_factory=list)
    encoded: bytes | None = None
    skip_reasons: dict[str, int] = field(default_factory=dict)
