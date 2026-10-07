"""Bounded regular-file access and durable publication, independent of the CLI."""

import hashlib
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from ..errors import CatalogueError, SourceError
from . import paths


CHUNK = 1024 * 1024


@contextmanager
def open_regular_file(path: Path) -> Iterator[BinaryIO]:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))

    try:

        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise CatalogueError("Not a regular file", path=path)

        with os.fdopen(fd, "rb", closefd=False) as stream:
            yield stream

    finally:
        os.close(fd)


def get_file_stamp(path: Path) -> tuple[int, ...]:
    info = path.lstat()

    if not stat.S_ISREG(info.st_mode):
        raise CatalogueError("Not a regular file", path=path)

    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def compute_sha256(path: Path) -> str:

    with open_regular_file(path) as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_sha256(path: Path, digest: str, *, error: str) -> None:

    if compute_sha256(path) != digest:
        raise CatalogueError(f"{error}", path=path)


def read_bytes(path: Path, limit: int) -> bytes:

    with open_regular_file(path) as stream:
        data = stream.read(limit + 1)

    if len(data) > limit:
        raise CatalogueError(f"File exceeds {limit} bytes", path=path)

    return data


def sync_directory(path: Path) -> None:
    # Windows has no portable directory-fsync; fail before moving any inputs there.
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))

    try:
        os.fsync(fd)

    finally:
        os.close(fd)


class DirectorySync:
    """Collect changed directory chains and flush children before their parents."""

    def __init__(self) -> None:
        self.pending: set[Path] = set()

    def add(self, path: Path, *, through: Path) -> None:

        if path != through and through not in path.parents:
            raise ValueError("Directory sync boundary is not an ancestor")

        for parent in (path, *path.parents):
            self.pending.add(parent)

            if parent == through:
                break

    def flush(self) -> None:
        def get_directory_sort_key(path: Path) -> tuple[int, str]:
            return -len(path.parts), f"{path}"

        for path in sorted(self.pending, key=get_directory_sort_key):
            sync_directory(path)
            self.pending.remove(path)


def write_atomic(path: Path, data: bytes) -> None:
    fd, name = tempfile.mkstemp(prefix=paths.ATOMIC_WRITE_PREFIX, dir=path.parent)
    tmp = Path(name)

    try:

        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

        os.replace(tmp, path)
        sync_directory(path.parent)

    finally:
        tmp.unlink(missing_ok=True)


def copy_rom(source: BinaryIO, target: Path, *, expected_size: int) -> None:
    """Stream a ROM, verifying its declared size without a resource-size ceiling."""

    if expected_size <= 0:
        raise SourceError("Empty ROM" if expected_size == 0 else "Invalid ROM size")

    count = 0

    with target.open("xb") as output:

        while chunk := source.read(CHUNK):
            count += len(chunk)

            if count > expected_size:
                raise SourceError("ROM exceeds declared size")

            output.write(chunk)

        if count != expected_size:
            raise SourceError("Incomplete ROM copy")

        output.flush()
        os.fsync(output.fileno())


def ensure_directory(path: Path) -> None:
    # Reject symlinked ancestors rather than writing through them to another volume.

    for part in [*reversed(path.parents), path]:

        if part.is_symlink():
            raise CatalogueError("Symlink directory is not allowed", path=part)

    path.mkdir(parents=True, exist_ok=True)

    if not path.is_dir():
        raise CatalogueError("Not a directory", path=path)


def resolve_relative_file(root: Path, name: str) -> Path:
    path = root / name

    for parent in [path, *path.parents]:

        if parent.is_symlink():
            raise CatalogueError("Symlink destination is not allowed", path=parent)

        if parent == root:
            break

    current = root

    for component in Path(name).parts:

        if current.is_dir():

            for sibling in current.iterdir():

                if sibling.name.casefold() == component.casefold() and sibling.name != component:
                    raise CatalogueError("Case-insensitive destination collision", path=sibling)

        current = current / component

    return path


def check_existing(root: Path, name: str, digest: str) -> Path:
    path = resolve_relative_file(root, name)

    if path.exists():
        verify_sha256(path, digest, error="Existing image differs; refusing overwrite")

    return path
