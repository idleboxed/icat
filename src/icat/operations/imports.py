"""Stage input sources, then remove them only after verified publication."""

import logging
import shutil
from pathlib import Path

from ..errors import CatalogueError, SourceError
from ..io.files import resolve_relative_file, compute_sha256, get_file_stamp, sync_directory, verify_sha256
from ..roms.types import Rom, Source
from ..roms.staging import stage_source
from .state import Session
from ..io import paths as layout


logger = logging.getLogger(__name__)


def import_sources(session: Session) -> None:
    """Inspect and stage selected sources while recording prepared, duplicate and rejected games."""
    options, progress, operation = session.options, session.progress, session.operation
    src, dst, temp = session.src, session.dst, session.staging
    entries, diagnostics = session.entries, session.diagnostics
    header_fixer, md_identifier = session.header_fixer, session.md_identifier
    paths = session.inventory.candidates
    journal, record = operation.data, operation.record

    if src is not None:
        progress.start("scan", total=len(paths))

    by_hash = {entry["hash"]: dict(entry) for entry in entries}

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

            if source is not None:

                for rom in source.roms:
                    digest = rom.entry["hash"]

                    if digest in existing_hashes and rom.entry["platform"] != by_hash[digest]["platform"]:
                        raise SourceError("Conflicting platforms for identical indexed content", path=path)

            if source is None:
                journal["skipped_sources"].append(f"{path}")

        except SourceError as exc:
            # I/O, source mutation and unsafe catalogue operations remain fatal.
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
                session.identity_cache.put(rom)
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
    session.staged_hashes = set(unique)
    session.duplicate_paths = duplicate_paths
    session.identity_cache.save()


def remove_sources(session: Session) -> None:
    """Remove approved source files only after publication and unchanged-source verification."""
    progress, operation = session.progress, session.operation
    sources, unique, dst = session.sources, session.unique, session.dst

    moving_sources = []

    for source in sources:

        if any(rom.entry["hash"] in session.skipped_hashes for rom in source.roms):
            operation.data.setdefault("retained_sources", []).append(f"{source.path}")
            logger.warning("%s — source retained because an indexed ROM was skipped", source.path)

        else:
            moving_sources.append(source)

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
