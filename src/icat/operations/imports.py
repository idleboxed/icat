"""Stage input sources, then remove them only after verified publication."""

import logging
import shutil
from pathlib import Path

from ..errors import CatalogueError, SourceError
from ..io.files import resolve_relative_file, compute_sha256, get_file_stamp, sync_directory, verify_sha256
from ..metadata.genres import resolve_genre
from ..roms.types import Rom, Source
from ..roms.staging import stage_source
from .state import Session
from ..io import paths as layout


logger = logging.getLogger(__name__)


def import_sources(session: Session) -> None:
    options, progress, operation = session.options, session.progress, session.operation
    src, dst, temp = session.src, session.dst, session.staging
    entries, diagnostics = session.entries, session.diagnostics
    header_fixer, md_identifier = session.header_fixer, session.md_identifier
    paths = session.inventory.candidates
    journal, record = operation.data, operation.record

    if src is not None:
        progress.start("scan", total=len(paths))

    by_hash = {entry["hash"]: dict(entry) for entry in entries}

    for entry in entries:

        with diagnostics.capture_events("catalogue", entry["hash"]):
            genre = resolve_genre(entry.get("genre"), entry["image"], diagnostics, existing=True)

        if genre != entry.get("genre"):
            by_hash[entry["hash"]]["genre"] = genre
            operation.record_metadata_changes(entry["hash"], {"genre": "icat"})
            logger.info(
                "%s — genre %r -> %s", layout.quote_path(Path(entry["image"]).name), entry["genre"], genre,
            )

    existing_hashes = set(by_hash)
    by_name = {entry["image"].casefold(): entry["hash"] for entry in entries}
    sources: list[Source] = []
    unique: dict[str, Rom] = {}
    duplicate_paths: set[str] = set()
    prepared_games = 0
    record("scanning")

    for index, path in enumerate(paths):
        progress.set_item(f"{layout.quote_path(path.name)} — importing")
        journal["current_source"] = f"{path}"
        staging = Path(temp) / f"{index}"
        staging.mkdir()

        try:
            source = stage_source(
                path, staging, diagnostics=diagnostics, header_fixer=header_fixer,
                platforms=options.platforms, md_identifier=md_identifier,
            )

            if source is None:
                journal["skipped_sources"].append(f"{path}")

        except SourceError as exc:
            # Only content errors are recoverable here. I/O, source mutation,
            # publication and existing-catalogue failures still stop the run.
            shutil.rmtree(staging)
            journal["rejected_sources"].append({"path": f"{path}", "reason": f"{exc}"})
            record("scanning")
            logger.warning("%s — rejected; kept: %s", path, exc)
            source = None

        if source:

            if all(rom.entry["hash"] in by_hash for rom in source.roms):
                duplicate_paths.add(f"{source.path}")

            sources.append(source)
            journal["nes_header_fixes"].extend({"source": f"{path}", **fix} for fix in source.header_fixes)
            journal["sources"].append({
                "path": f"{source.path}", "sha256": source.digest,
                "images": [rom.entry["hash"] for rom in source.roms],
            })

            for rom in source.roms:
                digest = rom.entry["hash"]

                if digest in by_hash:

                    if rom.entry["platform"] != by_hash[digest]["platform"]:
                        raise CatalogueError("Conflicting platforms for identical content", path=path)

                    rom.entry = dict(by_hash[digest])

                else:
                    name = f"{rom.entry["platform"]}/{digest[:2]}/{rom.entry["image"]}"

                    if name.casefold() in by_name and by_name[name.casefold()] != digest:
                        raise CatalogueError(
                            "Filename collision; rename one source explicitly", path=layout.get_image_path(name),
                        )

                    rom.entry["image"] = name
                    by_hash[digest] = rom.entry
                    prepared_games += 1
                    by_name[name.casefold()] = digest

                unique.setdefault(digest, rom)

        progress.set_counts(
            prepared_games=prepared_games, duplicate_sources=len(duplicate_paths),
            rejected_sources=len(journal["rejected_sources"]), skipped_sources=len(journal["skipped_sources"]),
        )
        progress.advance()

    journal.pop("current_source", None)

    session.by_hash, session.existing_hashes = by_hash, existing_hashes
    session.sources, session.unique = sources, unique
    session.duplicate_paths = duplicate_paths


def remove_sources(session: Session) -> None:
    progress, operation = session.progress, session.operation
    sources, unique, dst = session.sources, session.unique, session.dst

    moving_sources = sources
    moving_hashes = sorted({rom.entry["hash"] for source in moving_sources for rom in source.roms})
    progress.start("move", total=2 * len(moving_sources) + len(moving_hashes))
    # Batch preflight avoids partial deletions on any already-known inconsistency.

    for source in moving_sources:
        progress.set_item(f"{layout.quote_path(source.path.name)} — checking")

        if get_file_stamp(source.path) != source.fingerprint or compute_sha256(source.path) != source.digest:
            raise CatalogueError("Source changed; sources were retained", path=source.path)

        progress.advance()

    for digest in moving_hashes:
        rom = unique[digest]
        progress.set_item(f"{layout.quote_path(layout.get_image_path(rom.entry["image"]))} — checking")
        target = resolve_relative_file(dst / layout.IMAGES_DIR, rom.entry["image"])
        verify_sha256(target, digest, error="Destination changed; sources were retained")
        progress.advance()

    for source in moving_sources:
        progress.set_item(f"{layout.quote_path(source.path.name)} — removing")

        with operation.track_removal(source.path, collection="removed", digest=source.digest):

            if get_file_stamp(source.path) != source.fingerprint:
                raise CatalogueError("Source changed before removal", path=source.path)

            source.path.unlink()
            sync_directory(source.path.parent)

        progress.advance()
