"""Bounded, read-only libchdr access through an already guarded file descriptor.

SPDX-License-Identifier: BSD-3-Clause
"""

import ctypes as ct
import os
import struct
from contextlib import ExitStack
from ctypes.util import find_library
from functools import cache
from pathlib import Path

from ...errors import CatalogueError, SourceError
from ...io.files import open_regular_file


FRAME_BYTES = 2448
MAX_HUNK_BYTES = 512 * 1024
MAX_MAP_BYTES = 16 * 1024 * 1024
MAX_READ_BYTES = 16 * 1024 * 1024
MAX_READ_CALLS = 8192

_Size = ct.CFUNCTYPE(ct.c_uint64, ct.c_void_p)
_Read = ct.CFUNCTYPE(ct.c_size_t, ct.c_void_p, ct.c_size_t, ct.c_size_t, ct.c_void_p)
_Close = ct.CFUNCTYPE(ct.c_int, ct.c_void_p)
_Seek = ct.CFUNCTYPE(ct.c_int, ct.c_void_p, ct.c_int64, ct.c_int)


class _CoreFile(ct.Structure):
    # Stable legacy core_file ABI, retained by current libchdr releases.
    _fields_ = [("argp", ct.c_void_p), ("fsize", _Size), ("fread", _Read), ("fclose", _Close), ("fseek", _Seek)]


@cache
def load_library() -> ct.CDLL:
    """Load only when CHD is used; importing icat never requires a native library."""
    candidates = [find_library("chdr"), "libchdr.so.0", "libchdr.dylib", "chdr.dll", "libchdr.dll"]

    for name in dict.fromkeys(candidates):

        if not name:
            continue

        try:
            library = ct.CDLL(name)

        except OSError:
            continue

        signatures = {
            "chd_open_core_file": (ct.c_int, [ct.POINTER(_CoreFile), ct.c_int, ct.c_void_p, ct.POINTER(ct.c_void_p)]),
            "chd_close": (None, [ct.c_void_p]),
            "chd_read": (ct.c_int, [ct.c_void_p, ct.c_uint32, ct.c_void_p]),
            "chd_get_metadata": (ct.c_int, [
                ct.c_void_p, ct.c_uint32, ct.c_uint32, ct.c_void_p, ct.c_uint32,
                ct.POINTER(ct.c_uint32), ct.POINTER(ct.c_uint32), ct.POINTER(ct.c_uint8),
            ]),
            "chd_error_string": (ct.c_char_p, [ct.c_int]),
        }

        try:

            for symbol, (result, arguments) in signatures.items():
                function = getattr(library, symbol)
                function.restype, function.argtypes = result, arguments

        except AttributeError:
            continue

        return library

    raise CatalogueError("CHD import requires libchdr; on Ubuntu/Debian install the libchdr0 package")


class ChdReader:
    """Read metadata and individual CD hunks, never precache or expand the whole image."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.read_bytes = 0
        self.read_calls = 0
        self.stack = ExitStack()
        self.handle = ct.c_void_p()
        self.callback_error: BaseException | None = None
        self.cached_hunk: tuple[int, bytes] | None = None

    def __enter__(self) -> "ChdReader":
        def get_file_size(_opaque: int | None) -> int:
            return self.size

        def close_native_stream(_opaque: int | None) -> int:
            return 0

        try:
            self.stream = self.stack.enter_context(open_regular_file(self.path))
            self.size = os.fstat(self.stream.fileno()).st_size
            self._read_header()
            self.library = load_library()
            self.core = _CoreFile(
                None, _Size(get_file_size), _Read(self._read_callback),
                _Close(close_native_stream), _Seek(self._seek_callback),
            )
            # Python owns the descriptor; the native fclose callback intentionally does nothing.
            self.stack.callback(self._close)
            self._check(self.library.chd_open_core_file(ct.byref(self.core), 1, None, ct.byref(self.handle)))
            return self

        except BaseException:
            self.stack.close()
            raise

    def __exit__(self, *exc: object) -> None:
        self.stack.close()

    def _close(self) -> None:

        if self.handle:
            self.library.chd_close(self.handle)
            self.handle = ct.c_void_p()

        self.core = None
        self.cached_hunk = None

    def _read(self, count: int) -> bytes:
        self.read_calls += 1

        if self.read_calls > MAX_READ_CALLS or count > MAX_READ_BYTES - self.read_bytes:
            raise SourceError("CHD platform probe exceeds its bounded read budget", path=self.path)

        data = self.stream.read(count)
        self.read_bytes += len(data)
        return data

    def _read_callback(self, output: int, size: int, count: int, _opaque: int | None) -> int:

        if self.callback_error is not None or not size:
            return 0

        try:
            data = self._read(size * count)
            ct.memmove(output, data, len(data))
            return len(data) // size

        except BaseException as exc:
            # Exceptions cannot cross a C callback. Re-raise them immediately after the native call.
            self.callback_error = exc
            return 0

    def _seek_callback(self, _opaque: int | None, offset: int, whence: int) -> int:

        if self.callback_error is not None:
            return -1

        try:
            self.stream.seek(offset, whence)
            return 0

        except BaseException as exc:
            self.callback_error = exc
            return -1

    def _check(self, result: int) -> None:

        if self.callback_error is not None:
            raise self.callback_error

        if result:
            detail = self.library.chd_error_string(result).decode("ascii", errors="replace")
            raise SourceError(f"Cannot read CHD with installed libchdr: {detail}", path=self.path)

    def _read_header(self) -> None:
        header = self._read(124)

        if len(header) < 16 or header[:8] != b"MComprHD":
            raise SourceError("Invalid CHD header", path=self.path)

        length, version = struct.unpack_from(">II", header, 8)

        if version not in (3, 4, 5):
            raise SourceError(f"Unsupported CHD version: {version}", path=self.path)

        if length != {3: 120, 4: 108, 5: 124}[version] or len(header) < length:
            raise SourceError("Invalid CHD header length", path=self.path)

        if version == 5:
            self.logical_bytes, map_offset, meta_offset, self.hunk_bytes, unit = struct.unpack_from(
                ">QQQII", header, 32,
            )
            parent = any(header[104:124])

            if unit != FRAME_BYTES:
                raise SourceError("CHD is not a CD image (2448-byte frames required)", path=self.path)

        else:
            self.logical_bytes, meta_offset = struct.unpack_from(">QQ", header, 28)
            self.hunk_bytes = struct.unpack_from(">I", header, 76 if version == 3 else 44)[0]
            flags = struct.unpack_from(">I", header, 16)[0]
            parent = bool(flags & 1) or any(header[100:120] if version == 3 else header[68:88])

            if version == 3:
                parent = parent or any(header[60:76])

        if parent:
            raise SourceError("Parent-dependent CHD is not a self-contained image", path=self.path)

        if not 0 < self.hunk_bytes <= MAX_HUNK_BYTES or self.hunk_bytes % FRAME_BYTES:
            raise SourceError("Unsupported CHD CD hunk size", path=self.path)

        if not self.logical_bytes or self.logical_bytes % FRAME_BYTES:
            raise SourceError("Invalid CHD CD logical size", path=self.path)

        hunks = (self.logical_bytes + self.hunk_bytes - 1) // self.hunk_bytes

        if hunks * 16 > MAX_MAP_BYTES:
            raise SourceError("CHD hunk map exceeds the probe memory budget", path=self.path)

        if not length <= meta_offset < self.size:
            raise SourceError("Missing or invalid CHD CD metadata offset", path=self.path)

        if version == 5:

            if not length <= map_offset < self.size:
                raise SourceError("Invalid CHD hunk map offset", path=self.path)

            if any(header[16:32]):
                self.stream.seek(map_offset)
                compressed_size = self._read(4)

                if len(compressed_size) != 4:
                    raise SourceError("Truncated CHD hunk map", path=self.path)

                map_bytes = int.from_bytes(compressed_size, "big")

                if map_bytes > MAX_MAP_BYTES or map_offset + 16 + map_bytes > self.size:
                    raise SourceError("Invalid or oversized CHD compressed hunk map", path=self.path)

        elif struct.unpack_from(">I", header, 24)[0] != hunks:
            raise SourceError("Invalid CHD hunk count", path=self.path)

        self.stream.seek(0)

    def read_metadata(self, tag: bytes) -> bytes | None:
        """Return the first bounded record with this four-byte tag."""

        if len(tag) != 4:
            raise ValueError("CHD metadata tags must contain four bytes")

        buffer = ct.create_string_buffer(1024)
        length = ct.c_uint32()
        result = self.library.chd_get_metadata(
            self.handle, int.from_bytes(tag, "big"), 0, buffer, len(buffer), ct.byref(length), None, None,
        )

        if result == 19 and self.callback_error is None:  # CHDERR_METADATA_NOT_FOUND
            return None

        self._check(result)

        if length.value > len(buffer):
            raise SourceError("Oversized CHD track metadata", path=self.path)

        return buffer.raw[:length.value].rstrip(b"\0")

    def read_frame(self, number: int) -> bytes:
        """Read a single 2448-byte CD frame, caching at most one decompressed hunk."""
        position = number * FRAME_BYTES

        if number < 0 or position + FRAME_BYTES > self.logical_bytes:
            raise SourceError("CHD frame outside the image", path=self.path)

        hunk, offset = divmod(position, self.hunk_bytes)

        if self.cached_hunk is None or self.cached_hunk[0] != hunk:
            buffer = ct.create_string_buffer(self.hunk_bytes)
            self._check(self.library.chd_read(self.handle, hunk, buffer))
            self.cached_hunk = hunk, buffer.raw

        return self.cached_hunk[1][offset:offset + FRAME_BYTES]
