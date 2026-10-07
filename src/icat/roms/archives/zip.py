"""Bounded ZIP extraction using a retained, validated source descriptor."""


import logging
import lzma
import stat
import zipfile
import zlib
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .selection import choose_rom
from ...errors import SourceError
from ...reporting.diagnostics import Diagnostics
from ...io.files import copy_rom, open_regular_file


logger = logging.getLogger(__name__)


@contextmanager
def open_zip(path: Path) -> Iterator[zipfile.ZipFile]:
    try:

        with open_regular_file(path) as stream, zipfile.ZipFile(stream) as archive:
            yield archive

    except (
        zipfile.BadZipFile, NotImplementedError, lzma.LZMAError, zlib.error, EOFError, UnicodeDecodeError
    ) as exc:
        raise SourceError(f"Invalid or unsupported ZIP archive ({type(exc).__name__})", path=path) from exc


def extract_rom(
    path: Path, target: Path, *, extensions: Collection[str], unsupported: Collection[str],
    diagnostics: Diagnostics | None = None,
) -> str | None:
    """Extract one validated ZIP member into a fixed file, never its archive path."""
    diagnostics = diagnostics or Diagnostics()

    with open_zip(path) as archive:
        members = archive.infolist()
        chosen = []
        seen = set()

        for member in members:
            name = PurePosixPath(member.filename)

            if name.is_absolute() or ".." in name.parts or "\\" in member.filename or ":" in member.filename:
                raise SourceError("Unsafe ZIP member", path=member.filename)

            if name in seen:
                raise SourceError("Duplicate ZIP member", path=member.filename)

            seen.add(name)
            mode = member.external_attr >> 16

            if stat.S_ISLNK(mode):
                raise SourceError("ZIP symlink", path=member.filename)

            if member.is_dir():
                continue

            suffix = name.suffix.lower()

            if suffix in unsupported:
                diagnostics.emit("source", "unsupported_archive_components", path=f"{path}")
                logger.warning("%s — unsupported archive contents; kept", path)
                return None

            if suffix == ".md":

                if member.flag_bits & 1:
                    raise SourceError("Encrypted ZIP member", path=member.filename)

                with archive.open(member) as content:

                    if content.read(512)[256:260] != b"SEGA":
                        continue

            if suffix in extensions:
                chosen.append(member)

        selected = choose_rom([member.filename for member in chosen], path, diagnostics)

        if selected is None:
            return None

        member = next(member for member in chosen if member.filename == selected)

        if member.flag_bits & 1 or member.file_size <= 0:
            raise SourceError("Encrypted, empty or invalid-size ZIP member", path=member.filename)

        with archive.open(member) as content:
            copy_rom(content, target, expected_size=member.file_size)

        return PurePosixPath(member.filename).name
