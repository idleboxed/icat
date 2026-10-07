"""Lock, publish, synchronize and read back the destination catalogue."""


import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..catalogue.codec import LIMIT, encode
from ..errors import CatalogueError
from ..io.files import (
    DirectorySync,
    write_atomic,
    check_existing,
    ensure_directory,
    read_bytes,
    resolve_relative_file,
    sync_directory,
    verify_sha256,
)
from ..roms.types import Rom
from .state import Session
from ..io import paths as layout


@contextmanager
def lock_destination(dst: Path) -> Iterator[None]:
    path = dst / layout.LOCK_FILE

    try:

        with path.open("x", encoding="ascii") as stream:
            stream.write(f"pid={os.getpid()}\n")
            stream.flush()
            os.fsync(stream.fileno())

    except FileExistsError:
        raise CatalogueError("Destination is locked; inspect interrupted/concurrent sync first", path=path) from None

    try:
        sync_directory(dst)
        yield

    finally:
        path.unlink()
        sync_directory(dst)

def check_catalogue(path: Path, original: bytes | None) -> None:
    path = resolve_relative_file(path.parent, path.name)
    current = read_bytes(path, LIMIT) if path.exists() else None

    if current != original:
        raise CatalogueError("Catalogue was modified during sync; refusing further changes", path=path)

def publish_catalogue(path: Path, encoded: bytes, *, original: bytes | None) -> None:
    check_catalogue(path, original)

    if encoded != original:
        write_atomic(path, encoded)

    if read_bytes(path, LIMIT) != encoded:
        raise CatalogueError("Catalogue readback failed", path=path)

def publish_image(rom: Rom, dst: Path, directories: DirectorySync, *, indexed: bool) -> None:
    target = check_existing(dst / layout.IMAGES_DIR, rom.entry["image"], rom.entry["hash"])

    if target.exists():
        # An unindexed file may remain after an interrupted publication. Its
        # directory chain still needs a durability boundary before indexing it.

        if not indexed:
            directories.add(target.parent, through=dst)

        return

    if target == rom.path:
        raise CatalogueError("Indexed image is missing", path=target)

    ensure_directory(target.parent)
    # The private staging tree is on the same filesystem; catalogue is published last.
    os.replace(rom.path, target)
    directories.add(target.parent, through=dst)
    verify_sha256(target, rom.entry["hash"], error="Image verification failed")


def publish_games(session: Session) -> None:
    progress, operation, dst = session.progress, session.operation, session.dst
    by_hash, unique, metadata = session.by_hash, session.unique, session.metadata
    existing_hashes = session.existing_hashes
    catalogue_path, original = session.catalogue_path, session.original
    journal, record = operation.data, operation.record

    def get_entry_sort_key(entry: dict) -> tuple[str, str]:
        return entry["title"].casefold(), entry["hash"]

    ordered = sorted(by_hash.values(), key=get_entry_sort_key)
    encoded = encode(ordered)
    record("imports_prepared")
    progress.start("publish", total=len(unique) + 1)
    record("publishing")
    directories = DirectorySync()

    for rom in unique.values():
        progress.set_item(
            f"{layout.quote_path(layout.get_image_path(rom.entry["image"]))} — cataloguing and verifying"
        )
        journal["current_image"] = rom.entry["image"]
        publish_image(rom, dst, directories, indexed=rom.entry["hash"] in existing_hashes)
        result = metadata[rom.entry["hash"]]
        digest = rom.entry["hash"]
        thumb = resolve_relative_file(dst / layout.THUMBS_DIR, layout.get_thumbnail_name(digest))

        if result.thumbnail and not thumb.exists():
            ensure_directory(thumb.parent)
            write_atomic(thumb, result.thumbnail)
            directories.add(thumb.parent.parent, through=dst)

            if read_bytes(thumb, 1024 * 1024) != result.thumbnail:
                raise CatalogueError("Thumbnail verification failed", path=thumb)

            operation.record_metadata_changes(
                digest, {"thumbnail": result.field_sources.get("thumbnail", "unknown")}, result.sources,
                identifiers=result.identifiers,
            )

        progress.advance()

    journal.pop("current_image", None)
    progress.set_item(f"{layout.quote_path(layout.CATALOGUE_FILE)} — syncing and cataloguing")
    directories.flush()
    publish_catalogue(catalogue_path, encoded, original=original)
    record("published")
    progress.set_counts(catalogue_entries=len(ordered))
    progress.advance()

    session.ordered, session.encoded = ordered, encoded
