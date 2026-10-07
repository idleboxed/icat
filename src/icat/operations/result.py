"""Operation results and durable final summary."""

import logging
from dataclasses import dataclass, replace
from pathlib import Path

from ..metadata.normalization import is_description_missing
from .state import Session
from ..io import paths as layout


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Result:
    imported: int
    total: int
    removed: int
    missing_descriptions: int
    missing_thumbnails: int
    trashed: int = 0
    rejected: int = 0
    excluded: int = 0  # Legacy report counters; automatic exclusions are disabled.
    accepted_sources: int = 0
    duplicate_sources: int = 0
    skipped_sources: int = 0
    other_source_files: int = 0
    updated_existing: int = 0
    updated_new: int = 0
    other_genres: int = 0
    excluded_files_removed: int = 0
    nes_headers_fixed: int = 0
    metadata_status: str = "complete"
    metadata_completeness: str = "complete"
    catalogue_changed: bool = False
    mode: str = "COPY"
    journal_path: Path | None = None
    duration_seconds: float = 0

def log_result(result: Result) -> None:
    status = "COMPLETED WITH REJECTIONS" if result.rejected else "COMPLETED"
    elapsed = int(result.duration_seconds)
    logger.info(
        "%s · exit %d · elapsed %02d:%02d", status, 3 if result.rejected else 0, elapsed // 60, elapsed % 60,
        extra={"icat_summary": status},
    )
    logger.info("Games: %d new; %d existing updated; %d total", result.imported, result.updated_existing, result.total)

    if result.mode in {"COPY", "MOVE"}:
        logger.info(
            "Sources: %d accepted; %d duplicate sources; %d skipped candidates; %d rejected; %d other files",
            result.accepted_sources, result.duplicate_sources, result.skipped_sources,
            result.rejected, result.other_source_files,
        )

        if result.duplicate_sources:
            logger.info("Duplicate sources are included in accepted sources; they add no new game.")

    logger.info("Removed: %d source files; %d trash games", result.removed, result.trashed)

    if result.mode == "TRASH RESET":
        logger.info("Trash preferences reset; no game files removed.")

    elif result.catalogue_changed:
        logger.info("Catalogue saved and verified.")

    else:
        logger.info("%s — unchanged", Path(layout.CATALOGUE_FILE))

    if not result.mode.startswith("TRASH"):
        logger.info(
            "Optional metadata missing: %d descriptions; %d thumbnails",
            result.missing_descriptions, result.missing_thumbnails,
        )
        logger.info(
            "Metadata sources: %s; catalogue metadata: %s", result.metadata_status, result.metadata_completeness,
        )
        logger.info(
            "Metadata changed existing/new records: %d/%d; Other genres: %d",
            result.updated_existing, result.updated_new, result.other_genres,
        )

    if result.nes_headers_fixed:
        logger.info("Verified NES header repairs: %d", result.nes_headers_fixed)

    if result.rejected:
        logger.warning(
            "%d rejected source files; originals retained. Next: inspect their errors before retrying.",
            result.rejected,
        )

    if result.journal_path is not None:
        logger.info("%s — saved", result.journal_path)


def finish(session: Session) -> Result:
    options, progress, operation = session.options, session.progress, session.operation
    dst, unique, by_hash = session.dst, session.unique, session.by_hash
    ordered, encoded, original = session.ordered, session.encoded, session.original
    trash_entries, existing_hashes = session.trash_entries, session.existing_hashes
    sources, duplicate_paths = session.sources, session.duplicate_paths
    inventory, paths = session.inventory, session.inventory.candidates
    diagnostics, journal = session.diagnostics, operation.data

    progress.start("finalize")
    missing_images = sum(
        not (dst / layout.THUMBS_DIR / layout.get_thumbnail_name(digest)).is_file() for digest in unique
    )
    result = Result(
        len(set(unique) - existing_hashes),
        len(ordered),
        len(journal["removed"]),
        sum(is_description_missing(by_hash[digest]) for digest in unique),
        missing_images,
        len(trash_entries),
        len(journal["rejected_sources"]),
    )
    counts = diagnostics.get_snapshot()["counts"]
    skip_reasons = {
        reason: counts.get("transport", {}).get(f"source.{reason}", 0)
        for reason in ("multiple_roms", "no_supported_roms", "unsupported_archive_components",
                       "missing_mega_drive_signature")
        if counts.get("transport", {}).get(f"source.{reason}", 0)
    }
    journal["metadata_status"] = "partial" if any(
        values.get("lookup.unavailable", 0) or values.get("lookup.partial", 0)
        or values.get("lookup.offline_miss", 0) for values in counts.values()
    ) else "complete"
    journal["summary"] = {
        "imported": result.imported, "total": result.total, "removed": result.removed,
        "rejected": result.rejected, "skipped": len(journal["skipped_sources"]), "trashed": result.trashed,
        "excluded": result.excluded,
        "excluded_files_removed": len(journal["excluded_removed"]),
        "missing": {
            **{
                field: sum(entry[field] is None for entry in ordered)
                for field in ("region", "year", "players", "genre")
            },
            "description": result.missing_descriptions, "thumbnail": result.missing_thumbnails,
            "resolved_title": sum(entry["title"] == Path(entry["image"]).stem for entry in ordered),
        },
        "games_changed": len(journal["metadata_fields"]),
        "existing_games_changed": len(existing_hashes & journal["metadata_fields"].keys()),
        "new_games_changed": len(journal["metadata_fields"].keys() - existing_hashes),
        "genre_other": sum(entry["genre"] == "Other" for entry in ordered),
        "catalogue_changed": bool(trash_entries) or encoded != original,
        "nes_headers_fixed": len(journal["nes_header_fixes"]),
        "skipped_reasons": skip_reasons,
        "accepted_sources": len(sources),
        "duplicate_sources": len(duplicate_paths & {f"{source.path}" for source in sources}),
        "other_source_files": inventory.files - len(paths),
    }
    journal["metadata_completeness"] = "partial" if (
        any(journal["summary"]["missing"].values()) or journal["summary"]["genre_other"]
    ) else "complete"
    journal["state"] = "complete_with_rejections" if journal["rejected_sources"] else "complete"
    result = replace(
        result, accepted_sources=len(sources), duplicate_sources=journal["summary"]["duplicate_sources"],
        skipped_sources=len(journal["skipped_sources"]), other_source_files=inventory.files - len(paths),
        updated_existing=journal["summary"]["existing_games_changed"],
        updated_new=journal["summary"]["new_games_changed"], other_genres=journal["summary"]["genre_other"],
        excluded_files_removed=len(journal["excluded_removed"]),
        nes_headers_fixed=len(journal["nes_header_fixes"]),
        metadata_status=journal["metadata_status"], metadata_completeness=journal["metadata_completeness"],
        catalogue_changed=journal["summary"]["catalogue_changed"], mode=options.mode, journal_path=operation.path,
    )

    session.skip_reasons = skip_reasons
    return result


def finish_trash(session: Session) -> Result:
    options, progress, operation = session.options, session.progress, session.operation
    entries, trash_entries, journal = session.entries, session.trash_entries, operation.data

    progress.start("finalize")
    journal["summary"] = {
        "trashed": len(trash_entries), "total": len(entries), "trash_reset": options.trash_reset,
    }
    journal["state"] = "complete"
    return Result(
        0, len(entries), 0, 0, 0, trashed=len(trash_entries), mode=options.mode,
        catalogue_changed=bool(trash_entries), journal_path=operation.path,
    )
