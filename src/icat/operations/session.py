"""Acquire session resources and execute phases in their safety-critical order."""

import logging
import tempfile
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from time import monotonic

from ..catalogue.codec import LIMIT, decode
from ..reporting.diagnostics import Diagnostics
from ..io.files import ensure_directory, read_bytes, resolve_relative_file, sync_directory
from ..reporting.journal import RunJournal
from ..roms.formats.mega_drive import MegaDriveIdentifier
from ..metadata.provider import Provider
from ..roms.formats.nes_headers import NesHeaderFixer
from ..catalogue.preferences import Preferences
from ..reporting.progress import RunProgress, Step
from ..roms.types import SourceInventory
from ..roms.discovery import discover_sources
from .state import Session
from .options import Options, validate_options
from .result import Result, finish, finish_trash, log_result
from .imports import import_sources, remove_sources
from .metadata import update_metadata
from .publication import lock_destination, publish_games
from .trash import apply_trash
from ..io import paths as layout


logger = logging.getLogger(__name__)


def run(
    options: Options, provider: Provider, *, header_fixer: NesHeaderFixer | None = None,
    md_identifier: MegaDriveIdentifier | None = None,
) -> Result:
    steps = [Step("prepare", "Prepare & list files")]

    if options.trash or options.trash_reset:
        steps.append(Step("trash", "Clean trash" if options.trash else "Reset trash preferences"))

    if not options.trash_only:

        if options.src is not None:
            steps.append(Step("scan", "Import sources", "files"))

        steps.extend((
            Step("validate", "Validate existing catalogue"),
            Step("metadata", "Fetch metadata", "games"),
            Step("publish", "Catalogue & verify"),
        ))

    if options.move_roms:
        steps.append(Step("move", "Verify & remove sources"))

    steps.append(Step("finalize", "Finalize & save report"))

    with RunProgress(tuple(steps)) as progress:
        result = _run(options, provider, progress, header_fixer, md_identifier)
        progress.rejected = bool(result.rejected)

    result = replace(result, duration_seconds=monotonic() - progress.started)
    log_result(result)
    return result


def _run(
    options: Options, provider: Provider, progress: RunProgress, header_fixer: NesHeaderFixer | None,
    md_identifier: MegaDriveIdentifier | None,
) -> Result:
    progress.start("prepare")
    src, dst, cache, logs = validate_options(options)
    logger.info(
        "Mode: %s; source: %s; destination: %s; cache: %s; logs: %s",
        options.mode, layout.quote_path(src.name) if src is not None else "none",
        layout.quote_path(dst), layout.quote_path(cache), layout.quote_path(logs),
        extra={"icat_mode": options.mode},
    )

    if options.move_roms:
        logger.warning(
            "MOVE deletes each accepted archive in full, including unselected variants, filtered platforms "
            "and companion files, only after verified cataloguing. Rejected sources are kept."
        )

    ensure_directory(dst)
    ensure_directory(cache)
    log_root = logs / options.log_command
    ensure_directory(log_root)
    # Durability must be supported before any source is moved/deleted.
    sync_directory(dst.parent)
    config = {
        "source": f"{src}" if src is not None else None, "destination": f"{dst}", "cache": f"{cache}",
        "logs": f"{logs}", "mode": options.mode,
        "concurrency": options.concurrency,
        "move_roms": options.move_roms, "trash": options.trash, "trash_reset": options.trash_reset,
        "fix_nes_headers": options.fix_nes_headers,
        "platforms": list(options.platforms) if options.platforms is not None else None,
        **(provider.get_settings() if isinstance(provider, Provider) else {}),
    }
    diagnostics = getattr(provider, "diagnostics", Diagnostics())
    diagnostics.reset()

    if md_identifier is None and isinstance(provider, Provider):
        md_identifier = provider.md_identifier

    if md_identifier is not None:
        md_identifier.database.http.diagnostics = diagnostics
        md_identifier.database.datasets.diagnostics = diagnostics

    if not options.fix_nes_headers:
        header_fixer = None

    elif header_fixer is None and isinstance(provider, Provider) and provider.sources:
        first = provider.sources[0]
        header_fixer = NesHeaderFixer(first.http, first.datasets)

    if header_fixer is not None:
        header_fixer.http.diagnostics = diagnostics
        header_fixer.datasets.diagnostics = diagnostics

    with (
        RunJournal(log_root, config, diagnostics) as operation,
        lock_destination(dst),
        (nullcontext(None) if options.trash_only else tempfile.TemporaryDirectory(
            prefix=layout.STAGING_PREFIX, dir=dst,
        )) as temp,
    ):
        inventory = (
            discover_sources(src, platforms=options.platforms, diagnostics=diagnostics)
            if src is not None else SourceInventory(0, [])
        )
        paths = inventory.candidates
        progress.set_counts(source_files=inventory.files, candidate_sources=len(paths))

        if not options.trash_only:

            for name in (layout.IMAGES_DIR, layout.THUMBS_DIR):
                ensure_directory(dst / name)

        catalogue_path = resolve_relative_file(dst, layout.CATALOGUE_FILE)
        original = read_bytes(catalogue_path, LIMIT) if catalogue_path.exists() else None
        entries = decode(original) if original is not None else []
        progress.set_counts(catalogue_entries=len(entries))
        preferences = Preferences.load(dst.parent) if options.trash or options.trash_reset else None

        if preferences is not None:
            preferences.check_unchanged()

        session = Session(
            options=options, src=src, dst=dst, staging=Path(temp) if temp is not None else None,
            provider=provider, diagnostics=diagnostics, progress=progress, operation=operation,
            catalogue_path=catalogue_path, original=original, entries=entries, inventory=inventory,
            preferences=preferences, header_fixer=header_fixer, md_identifier=md_identifier,
        )
        apply_trash(session)

        if options.trash_only:
            return finish_trash(session)

        import_sources(session)
        update_metadata(session)
        publish_games(session)

        if options.move_roms:
            remove_sources(session)

        result = finish(session)

    for reason, count in session.skip_reasons.items():
        logger.warning("Skipped sources: %s=%d", reason, count)

    return result
