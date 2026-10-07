"""Prepare a private ROM copy and verify the original source has not changed."""


import logging
import os
from collections.abc import Collection
from pathlib import Path

from ..errors import CatalogueError, SourceError
from ..catalogue.codec import is_valid_filename
from ..reporting.diagnostics import Diagnostics
from ..io.files import copy_rom, open_regular_file, compute_sha256, get_file_stamp
from .formats.mega_drive import MegaDriveIdentifier
from .formats.nes_headers import NesHeaderFixer
from .archives.sevenzip import extract_rom as extract_7z_rom
from .types import Source
from .platforms import ARCHIVES, UNSUPPORTED, get_selected_extensions
from .inspection import inspect_rom
from .archives.zip import extract_rom as extract_zip_rom
from ..io import paths


logger = logging.getLogger(__name__)


def stage_source(
    path: Path, staging: Path, *, diagnostics: Diagnostics | None = None,
    header_fixer: NesHeaderFixer | None = None, platforms: Collection[str] | None = None,
    md_identifier: MegaDriveIdentifier | None = None,
) -> Source | None:
    diagnostics = diagnostics or Diagnostics()
    extensions = get_selected_extensions(platforms)

    if path.suffix.lower() not in ARCHIVES and path.suffix.lower() not in extensions:
        return None

    before = get_file_stamp(path)
    digest = compute_sha256(path)

    if path.suffix.lower() == ".md":

        with open_regular_file(path) as stream:

            if stream.read(512)[256:260] != b"SEGA":
                diagnostics.emit("source", "missing_mega_drive_signature", path=f"{path}")
                logger.warning("%s — no Mega Drive signature; kept (possibly Markdown)", path)
                return None

    target = staging / paths.SINGLE_ROM_SLOT

    if path.suffix.lower() == ".zip":
        name = extract_zip_rom(
            path, target, extensions=extensions, unsupported=UNSUPPORTED | ARCHIVES, diagnostics=diagnostics,
        )

        if name is None:
            return None

    elif path.suffix.lower() == ".7z":
        name = extract_7z_rom(
            path, target, extensions=extensions, unsupported=UNSUPPORTED | ARCHIVES,
            diagnostics=diagnostics,
        )

        if name is None:
            return None

    else:

        with open_regular_file(path) as stream:
            copy_rom(stream, target, expected_size=os.fstat(stream.fileno()).st_size)

        name = path.name

    if not is_valid_filename(name):
        raise SourceError("Non-portable ROM filename", path=name)

    fix = header_fixer.repair_staged(target, name=name) if header_fixer is not None else None
    rom = inspect_rom(target, name, diagnostics=diagnostics, md_identifier=md_identifier)

    if platforms is not None and rom.entry["platform"] not in platforms:
        diagnostics.emit("source", "platform_filtered", path=f"{path}", platform=rom.entry["platform"])
        logger.info("%s — detected %s; excluded by platform filter; kept", path, rom.entry["platform"])
        return None

    if fix and rom.entry["hash"] != fix["sha256"]:
        raise CatalogueError("Repaired ROM readback mismatch", path=path)

    if get_file_stamp(path) != before or compute_sha256(path) != digest:
        raise CatalogueError("Source changed while reading", path=path)

    if fix:
        diagnostics.emit("source", "nes_header_fixed", path=f"{path}", **fix)
        logger.warning(
            "%s — NES header fixed in staging: CHR %d -> %d KiB%s; source follows COPY/MOVE mode",
            paths.quote_path(name), fix["chr_bytes_before"] // 1024, fix["chr_bytes_after"] // 1024,
            "; battery enabled" if fix["battery_added"] else "",
        )

    return Source(path, before, digest, [rom], [fix] if fix else [])
