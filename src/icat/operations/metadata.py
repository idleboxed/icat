"""Validate existing images before looking up missing metadata."""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ..catalogue.codec import validate
from ..errors import CatalogueError
from ..io.files import check_existing, resolve_relative_file
from ..metadata.genres import resolve_genre
from ..metadata.types import Metadata
from ..metadata.provider import Provider
from ..metadata.normalization import is_description_missing, is_placeholder_title
from ..roms.inspection import inspect_rom
from .state import Session
from ..io import paths as layout


logger = logging.getLogger(__name__)


def update_metadata(session: Session) -> None:
    options, progress, operation = session.options, session.progress, session.operation
    entries, unique, by_hash = session.entries, session.unique, session.by_hash
    provider, diagnostics = session.provider, session.diagnostics
    dst, md_identifier = session.dst, session.md_identifier
    journal, record = operation.data, operation.record

    metadata: dict[str, Metadata] = {}
    record("validating_existing")
    inspected = set()
    to_inspect = [entry for entry in entries if entry["hash"] not in unique]
    progress.start("validate", total=len(unique) + 2 * len(to_inspect))
    # Revisit stored ROMs too: metadata can become available after the input queue was moved.

    for entry in to_inspect:
        progress.set_item(f"{layout.quote_path(layout.get_image_path(entry["image"]))} — checking")
        path = resolve_relative_file(dst / layout.IMAGES_DIR, entry["image"])

        if not path.exists():
            raise CatalogueError("Indexed image is missing", path=path)

        rom = inspect_rom(
            path, path.name, diagnostics=diagnostics, diagnostic_path=path, md_identifier=md_identifier,
        )

        if rom.entry["hash"] != entry["hash"] or rom.entry["platform"] != entry["platform"]:
            raise CatalogueError("Indexed image identity/platform mismatch", path=path)

        rom.entry = dict(by_hash[entry["hash"]])
        unique[entry["hash"]] = rom
        inspected.add(entry["hash"])
        progress.advance()

    # Discover all conflicts before network requests or publishing any assets.

    for rom in unique.values():
        progress.set_item(f"{layout.quote_path(layout.get_image_path(rom.entry["image"]))} — checking")
        digest = rom.entry["hash"]

        if digest not in inspected:
            check_existing(dst / layout.IMAGES_DIR, rom.entry["image"], digest)

        rom.has_thumbnail = resolve_relative_file(dst / layout.THUMBS_DIR, layout.get_thumbnail_name(digest)).is_file()
        progress.advance()

    pending = list(unique.values())
    progress.start("metadata", total=len(pending))
    record("metadata")
    pool = ThreadPoolExecutor(max_workers=options.concurrency)
    results = None

    try:

        if isinstance(provider, Provider):
            results = provider.iter_lookup(pending, executor=pool, max_pending=options.concurrency)
            completed = ((pending[index].entry["hash"], result) for index, result in results)

        else:
            futures = {pool.submit(provider.lookup, rom): rom for rom in pending}
            completed = ((futures[future].entry["hash"], future.result()) for future in as_completed(futures))

        for count, (digest, result) in enumerate(completed, 1):
            metadata[digest] = result
            journal["metadata_completed"] = count
            progress.advance()
            entry = by_hash[digest]
            proposed = dict(entry)

            if is_placeholder_title(proposed.get("title")):
                proposed["title"] = Path(entry["image"]).stem
                logger.warning(
                    "%s — stored ZZZ title treated as missing; ROM retained",
                    layout.quote_path(Path(entry["image"]).name),
                )
                diagnostics.emit("metadata", "stored_placeholder", hash=digest)

            if is_description_missing(proposed):
                proposed["description"] = None

            try:

                for key, value in result.fields.items():

                    if key in {"title", "region", "year", "players", "genre", "description"} and (
                        proposed.get(key) is None
                        or (key == "title" and proposed.get("title") == Path(entry["image"]).stem)
                        or (key == "description" and is_description_missing(proposed))
                        or (key == "genre" and proposed.get(key) == "Other")
                    ):

                        if key == "genre":
                            value = resolve_genre(value, entry["image"], diagnostics)

                            if value is None:
                                continue

                        proposed[key] = value

                validate([proposed])

            except CatalogueError as exc:
                logger.warning(
                    "%s — invalid metadata ignored: %s",
                    layout.quote_path(Path(entry["image"]).name), exc.message,
                )

            else:
                changed = {
                    key: result.field_sources.get(key, "unknown")
                    for key in proposed if proposed[key] != entry.get(key)
                }

                if changed:
                    operation.record_metadata_changes(digest, changed, result.sources, identifiers=result.identifiers)

                entry.update(proposed)

            record("metadata")

    except BaseException:

        if isinstance(provider, Provider):
            provider.cancel()

        raise

    finally:
        try:

            if results is not None:
                results.close()

        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    session.metadata = metadata
