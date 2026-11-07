"""Validate existing images before looking up missing metadata."""

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path

from ..catalogue.codec import validate
from ..errors import CatalogueError, SourceError
from ..io.files import check_existing, compute_sha256, get_file_stamp, resolve_relative_file
from ..metadata.genres import normalize_genre, resolve_genre
from ..metadata.types import Metadata
from ..metadata.provider import Provider
from ..metadata.normalization import is_description_missing, is_placeholder_title
from ..roms.inspection import inspect_rom
from ..roms.platforms import EXTENSIONS
from ..roms.types import Rom
from .state import Session
from ..io import paths as layout


logger = logging.getLogger(__name__)


def needs_metadata(session: Session, rom: Rom) -> bool:
    """Plan lookups without reading ROM contents or changing stored fields."""

    if not isinstance(session.provider, Provider):
        return True

    candidate = replace(rom, entry={**rom.entry, "genre": normalize_genre(rom.entry.get("genre"))})
    return session.provider.needs_lookup(candidate)


def prepare_existing_rom(
    session: Session, entry: dict, path: Path, *, size: int, has_thumbnail: bool,
) -> tuple[Rom | None, dict | None]:
    """Reuse catalogue lookup identities; hash only uncached lookups or staged-source targets."""
    digest = entry["hash"]
    suffix = path.suffix.lower()

    if suffix not in EXTENSIONS:
        raise SourceError("Unsupported ROM", path=path)

    platform = EXTENSIONS[suffix]

    if (platform is not None and platform != entry["platform"]) or (
        suffix == ".chd" and entry["platform"] not in {"PSX", "SCD"}
    ):
        return None, {"reason": "Indexed image platform mismatch", "actual_platform": platform}

    if digest in session.staged_hashes:
        staged = session.unique[digest]

        if staged.size != size:
            return None, {"reason": "Indexed image size mismatch", "expected_size": staged.size, "actual_size": size}

        actual = compute_sha256(path)

        if actual != digest:
            return None, {"reason": "Indexed image identity mismatch", "actual_hash": actual}

    # Identical source bytes may have a different extension and lookup payload
    # (for example .unf versus .nes); use identities for the catalogue format.
    rom = session.identity_cache.get(path, entry)

    if rom is not None:

        if rom.size != size:
            return None, {"reason": "Indexed image size mismatch", "expected_size": rom.size, "actual_size": size}

        return rom, None

    rom = Rom(path, dict(entry), [], has_thumbnail=has_thumbnail, size=size)
    # Disc metadata uses the stored platform/title, not track identities. Other
    # complete records need no lookup identities and therefore no ROM read.

    if suffix == ".chd" or not needs_metadata(session, rom):
        return rom, None

    rom = inspect_rom(
        path, path.name, diagnostics=session.diagnostics, diagnostic_path=path, md_identifier=session.md_identifier,
    )

    if rom.entry["hash"] != digest or rom.entry["platform"] != entry["platform"]:
        return None, {
            "reason": "Indexed image identity/platform mismatch",
            "actual_hash": rom.entry["hash"], "actual_platform": rom.entry["platform"],
        }

    session.identity_cache.put(rom)
    return rom, None


def validate_existing_images(session: Session) -> None:
    """Check files cheaply and prepare missing lookup identities, not audit the collection."""
    progress, operation = session.progress, session.operation
    unique, by_hash = session.unique, session.by_hash
    diagnostics = session.diagnostics
    dst = session.dst
    journal, record = operation.data, operation.record
    image_root = dst / layout.IMAGES_DIR
    root_stat = image_root.stat()
    record("validating_existing")
    progress.start("validate", total=len(by_hash))

    for digest, entry in by_hash.items():
        issue = None
        progress.set_item(f"{layout.quote_path(layout.get_image_path(entry["image"]))} — checking")
        path = resolve_relative_file(image_root, entry["image"])
        has_thumbnail = resolve_relative_file(dst / layout.THUMBS_DIR, layout.get_thumbnail_name(digest)).is_file()

        if digest not in session.existing_hashes:
            check_existing(image_root, entry["image"], digest)
            rom = unique[digest]

        else:
            try:
                before = get_file_stamp(path)

            except FileNotFoundError:
                # Missing media/root is fatal, not thousands of individual skips.
                image_root.stat()
                issue = {"reason": "Indexed image is missing"}

            else:

                if before[2] == 0:
                    issue = {"reason": "Empty ROM"}

                else:
                    try:
                        rom, issue = prepare_existing_rom(
                            session, entry, path, size=before[2], has_thumbnail=has_thumbnail,
                        )

                    except SourceError as exc:
                        issue = {"reason": exc.message}

                if get_file_stamp(path) != before:
                    raise CatalogueError("Indexed image changed while reading", path=path)

                if issue is None:
                    session.image_stamps[digest] = before

        if issue is not None:
            session.skipped_hashes.add(digest)
            unique.pop(digest, None)
            journal["skipped_images"].append({"path": f"{path}", "hash": digest, **issue})
            logger.warning("%s — indexed ROM skipped; record and files retained: %s", path, issue["reason"])
            progress.set_counts(skipped_images=len(session.skipped_hashes))
            record("validating_existing")

        else:
            unique[digest] = rom
            rom.has_thumbnail = has_thumbnail

            if digest in session.existing_hashes:

                with diagnostics.capture_events("catalogue", digest):
                    genre = resolve_genre(entry.get("genre"), entry["image"], diagnostics, existing=True)

                if genre != entry.get("genre"):
                    logger.info(
                        "%s — genre %r -> %s", layout.quote_path(Path(entry["image"]).name), entry["genre"], genre,
                    )
                    entry["genre"] = genre
                    operation.record_metadata_changes(digest, {"genre": "icat"})

            rom.entry = dict(entry)

        progress.advance()

    if not os.path.samestat(root_stat, image_root.stat()):
        raise CatalogueError("Image directory changed during validation", path=image_root)

    session.identity_cache.save()


def apply_metadata(session: Session, digest: str, result: Metadata) -> None:
    """Apply local missing-field normalization and valid provider fields to one record."""
    entry = session.by_hash[digest]
    proposed = dict(entry)

    if is_placeholder_title(proposed.get("title")):
        proposed["title"] = Path(entry["image"]).stem
        logger.warning(
            "%s — stored ZZZ title treated as missing; ROM retained",
            layout.quote_path(Path(entry["image"]).name),
        )
        session.diagnostics.emit("metadata", "stored_placeholder", hash=digest)

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
                    value = resolve_genre(value, entry["image"], session.diagnostics)

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
            session.operation.record_metadata_changes(digest, changed, result.sources, identifiers=result.identifiers)

        entry.update(proposed)


def update_metadata(session: Session) -> None:
    """Fill missing metadata using reused identities, retaining skipped catalogue records."""
    validate_existing_images(session)
    options, progress, operation = session.options, session.progress, session.operation
    unique = session.unique
    provider = session.provider
    journal, record = operation.data, operation.record
    metadata = {digest: Metadata() for digest in unique}
    pending = [rom for rom in unique.values() if needs_metadata(session, rom)]
    pending_hashes = {rom.entry["hash"] for rom in pending}

    for digest in unique:

        if digest not in pending_hashes:
            apply_metadata(session, digest, metadata[digest])

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
            apply_metadata(session, digest, result)
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
