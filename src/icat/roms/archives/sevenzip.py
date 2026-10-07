"""Size-verified 7z cartridge extraction without creating archive paths.

SPDX-License-Identifier: BSD-3-Clause
"""

import logging
import lzma
import os
import shutil
import struct
import subprocess
import zlib
from collections.abc import Collection
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from py7zr import FileInfo, Py7zIO, SevenZipFile, WriterFactory
from py7zr.exceptions import (
    Bad7zFile,
    CrcError,
    DecompressionBombError,
    DecompressionError,
    PasswordRequired,
    UnsupportedCompressionMethodError,
)

from .selection import choose_rom
from ...errors import CatalogueError, SourceError
from ...reporting.diagnostics import Diagnostics
from ...io.files import open_regular_file


logger = logging.getLogger(__name__)


class _Writer(Py7zIO):
    def __init__(self, output: BinaryIO, expected: int, *, prefix_only: bool = False) -> None:
        self.output = output
        self.expected = expected
        self.prefix_only = prefix_only
        self.count = 0

    def write(self, data: bytes | bytearray) -> int:
        end = self.count + len(data)

        if end > self.expected:
            raise SourceError("7z member exceeds declared size")

        chunk = data[: max(0, 512 - self.count)] if self.prefix_only else data
        self.output.write(chunk)
        self.count = end
        return len(data)

    def read(self, size: int | None = None) -> bytes:
        return self.output.read(size)

    def seek(self, offset: int, whence: int = 0) -> int:
        return self.output.seek(offset, whence)

    def flush(self) -> None:
        self.output.flush()

    def size(self) -> int:
        return self.count


class _Factory(WriterFactory):
    def __init__(self, writers: dict[str, _Writer]) -> None:
        self.writers = writers
        self.created: set[str] = set()

    def create(self, filename: str) -> Py7zIO:

        if filename not in self.writers or filename in self.created:
            raise SourceError("Unexpected or duplicate 7z output", path=filename)

        self.created.add(filename)
        return self.writers[filename]

    def extract(self, archive: SevenZipFile) -> None:
        archive.extract(targets=list(self.writers), factory=self)

        if self.created != self.writers.keys() or any(
            writer.count != writer.expected for writer in self.writers.values()
        ):
            raise SourceError("Incomplete 7z extraction")


def _extract_with_7z(stream: BinaryIO, member: FileInfo, writer: _Writer) -> None:
    """Decode one selected member through 7-Zip when liblzma rejects valid LZMA parameters."""
    executable = shutil.which("7z")
    descriptor = stream.fileno()
    descriptor_path = Path(f"/proc/self/fd/{descriptor}")

    if executable is None:
        raise SourceError(
            "System 7z is required for LZMA parameters unsupported by Python", path=member.filename,
        )

    if not descriptor_path.exists():
        raise SourceError("System 7z fallback requires procfs file descriptors", path=member.filename)

    stream.seek(0)
    process = subprocess.Popen(
        [executable, "x", "-so", "--", f"{descriptor_path}", member.filename],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        pass_fds=(descriptor,),
    )

    try:

        if process.stdout is None:
            raise OSError("7z stdout pipe was not created")

        while chunk := process.stdout.read(1024 * 1024):
            writer.write(chunk)

        process.stdout.close()
        status = process.wait()

    except BaseException:
        process.kill()
        process.wait()
        raise

    if status != 0:
        raise SourceError("System 7z could not decode the LZMA member", path=member.filename)

    if writer.count != writer.expected:
        raise SourceError("Incomplete 7z extraction", path=member.filename)


def _validate_members(members: list[FileInfo]) -> None:
    seen = set()

    for member in members:
        filename = member.filename
        name = PurePosixPath(filename)
        # Require canonical names so the library cannot normalize two entries to
        # the same factory output. No archive name is ever used as a disk path.

        if (
            not filename
            or name.is_absolute()
            or ".." in name.parts
            or "\\" in filename
            or ":" in filename
            or any(ord(character) < 32 for character in filename)
            or name.as_posix() != filename.rstrip("/")
        ):
            raise SourceError("Unsafe 7z member", path=filename)

        if name in seen:
            raise SourceError("Duplicate 7z member", path=filename)

        seen.add(name)

        if member.is_symlink or not (member.is_file or member.is_directory):
            raise SourceError("7z symlink or special file", path=filename)

        if member.uncompressed < 0:
            raise SourceError("Invalid 7z member size", path=filename)


def _open_archive(stream: BinaryIO) -> SevenZipFile:
    try:
        return SevenZipFile(stream)

    except (TypeError, IndexError) as exc:
        # py7zr's header parser also uses these for unknown/truncated fields.
        # Do not catch them around our selection or extraction code.
        raise SourceError(f"Invalid 7z header ({type(exc).__name__})") from exc


def extract_rom(
    path: Path,
    target: Path,
    *,
    extensions: Collection[str],
    unsupported: Collection[str],
    diagnostics: Diagnostics | None = None,
) -> str | None:
    """Extract one game image into a fixed file; return its basename or skip.

    Solid blocks can decode ignored files too; there is no unpacked-size ceiling.
    Declared member sizes and CRC still have to match the decoded data.
    """
    diagnostics = diagnostics or Diagnostics()

    try:
        # Passing a stream keeps py7zr extraction single-threaded and on the
        # already-validated descriptor instead of reopening the source by name.

        with open_regular_file(path) as stream, _open_archive(stream) as archive:

            if archive.needs_password():
                raise SourceError("Encrypted 7z archive", path=path)

            members = archive.list()
            _validate_members(members)
            files = [member for member in members if not member.is_directory]

            if any(PurePosixPath(member.filename).suffix.lower() in unsupported for member in files):
                diagnostics.emit("source", "unsupported_archive_components", path=f"{path}")
                logger.warning("%s — unsupported archive contents; kept", path)
                return None

            chosen, markdown = [], []

            for member in files:
                suffix = PurePosixPath(member.filename).suffix.lower()

                if suffix == ".md" and suffix in extensions:
                    markdown.append(member)

                elif suffix in extensions:
                    chosen.append(member)

            if markdown:
                # Probe all .md headers in one pass, retaining only 512 bytes per
                # entry. Avoid one full solid-block decode per README.md.
                writers = {
                    member.filename: _Writer(BytesIO(), member.uncompressed, prefix_only=True) for member in markdown
                }

                try:
                    _Factory(writers).extract(archive)

                except lzma.LZMAError:
                    writers = {
                        member.filename: _Writer(BytesIO(), member.uncompressed, prefix_only=True)
                        for member in markdown
                    }

                    for member in markdown:
                        _extract_with_7z(stream, member, writers[member.filename])

                    diagnostics.emit(
                        "source", "sevenzip_external_decoder", path=f"{path}",
                        members=len(markdown), reason="unsupported_lzma_parameters",
                    )
                    logger.info("%s — system 7z decoded unsupported LZMA parameters", path)

                chosen.extend(
                    member for member in markdown if writers[member.filename].read(512)[256:260] == b"SEGA"
                )
                archive.reset()

            selected = choose_rom([member.filename for member in chosen], path, diagnostics)

            if selected is None:
                return None

            member = next(member for member in chosen if member.filename == selected)

            if member.uncompressed <= 0:
                raise SourceError("Empty 7z member", path=member.filename)

            with target.open("x+b") as output:
                writer = _Writer(output, member.uncompressed)

                try:
                    _Factory({member.filename: writer}).extract(archive)

                except lzma.LZMAError:
                    output.seek(0)
                    output.truncate()
                    writer = _Writer(output, member.uncompressed)
                    _extract_with_7z(stream, member, writer)
                    diagnostics.emit(
                        "source", "sevenzip_external_decoder", path=f"{path}", member=member.filename,
                        reason="unsupported_lzma_parameters",
                    )
                    logger.info("%s — system 7z decoded unsupported LZMA parameters", path)

                output.flush()
                os.fsync(output.fileno())

            return PurePosixPath(member.filename).name

    except PasswordRequired as exc:
        raise SourceError("Encrypted 7z archive", path=path) from exc

    except CatalogueError:
        raise

    except (
        Bad7zFile,
        CrcError,
        DecompressionError,
        DecompressionBombError,
        UnsupportedCompressionMethodError,
        lzma.LZMAError,
        zlib.error,
        EOFError,
        struct.error,
        ValueError,
    ) as exc:
        raise SourceError(f"Invalid or unsupported 7z archive ({type(exc).__name__})", path=path) from exc
