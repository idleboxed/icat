"""Compute metadata lookup identities without changing the stored ROM bytes.

SPDX-License-Identifier: BSD-3-Clause
"""

import hashlib
import zlib


COPIER_HEADER_BYTES = 512
SMD_BLOCK_BYTES = 16 * 1024
SNES_BANK_BYTES = 8192


class LookupNormalizer:
    """Hash a supported ROM representation using bounded temporary buffers."""

    def __init__(self, *, platform: str, header: bytes, size: int) -> None:
        self.kind: str | None = None
        self._size = size
        self._received_size = 0
        self._normalized_size = 0
        self._skip = 0
        self._group_size = 0
        self._signature = bytes(header[:4])
        self._seen_signature = bytearray()
        self._buffer = bytearray()
        self._sha1 = hashlib.sha1()
        self._crc32 = 0

        if platform == "SNES" and size > COPIER_HEADER_BYTES and size % SNES_BANK_BYTES == COPIER_HEADER_BYTES:
            self.kind = "snes-copier-header"
            self._skip = COPIER_HEADER_BYTES

        elif platform == "MD" and size >= COPIER_HEADER_BYTES + SMD_BLOCK_BYTES:

            if (size - COPIER_HEADER_BYTES) % SMD_BLOCK_BYTES == 0:
                self.kind = "smd-interleaved"
                self._skip = COPIER_HEADER_BYTES

        elif platform == "N64" and size >= 4:

            if self._signature == b"\x37\x80\x40\x12" and size % 2 == 0:
                self.kind = "n64-byteswap16"
                self._group_size = 2

            elif self._signature == b"\x40\x12\x37\x80" and size % 4 == 0:
                self.kind = "n64-byteswap32"
                self._group_size = 4

    def update(self, chunk: bytes | bytearray | memoryview) -> None:
        """Consume an original byte chunk without modifying it or retaining the whole chunk."""

        if self.kind is None:
            return

        data = memoryview(chunk).cast("B")
        self._received_size += len(data)

        if self._received_size > self._size:
            self._disable()
            return

        if self._group_size and len(self._seen_signature) < 4:
            self._seen_signature.extend(data[:4 - len(self._seen_signature)])

            if self._signature[:len(self._seen_signature)] != self._seen_signature:
                self._disable()
                return

        skipped = min(self._skip, len(data))
        self._skip -= skipped
        data = data[skipped:]

        if self.kind == "snes-copier-header":
            self._update_checksum(data)

        elif self.kind == "smd-interleaved":
            self._update_smd(data)

        else:
            self._update_swapped(data)

    def finish(self) -> tuple[tuple[str, int], str] | None:
        """Return complete transformed SHA-1/size and CRC, or None, without closing the stream."""

        if (
            self.kind is None
            or self._received_size != self._size
            or self._skip
            or self._buffer
            or not self._normalized_size
        ):
            return None

        return (self._sha1.hexdigest(), self._normalized_size), f"{self._crc32:08x}"

    def _disable(self) -> None:
        self.kind = None
        self._buffer.clear()

    def _update_checksum(self, data: bytes | bytearray | memoryview) -> None:
        self._sha1.update(data)
        self._crc32 = zlib.crc32(data, self._crc32)
        self._normalized_size += len(data)

    def _update_smd(self, data: memoryview) -> None:

        while data:
            taken = min(SMD_BLOCK_BYTES - len(self._buffer), len(data))
            self._buffer.extend(data[:taken])
            data = data[taken:]

            if len(self._buffer) < SMD_BLOCK_BYTES:
                return

            decoded = bytearray(SMD_BLOCK_BYTES)
            decoded[0::2] = self._buffer[SMD_BLOCK_BYTES // 2:]
            decoded[1::2] = self._buffer[:SMD_BLOCK_BYTES // 2]
            self._buffer.clear()

            if self._normalized_size == 0 and decoded[0x100:0x104] != b"SEGA":
                self._disable()
                return

            self._update_checksum(decoded)

    def _update_swapped(self, data: memoryview) -> None:
        group_size = self._group_size

        if self._buffer:
            taken = min(group_size - len(self._buffer), len(data))
            self._buffer.extend(data[:taken])
            data = data[taken:]

            if len(self._buffer) < group_size:
                return

            self._update_checksum(self._buffer[::-1])
            self._buffer.clear()

        complete = len(data) - len(data) % group_size

        for offset in range(0, complete, SMD_BLOCK_BYTES):
            block = data[offset:min(offset + SMD_BLOCK_BYTES, complete)]
            decoded = bytearray(len(block))

            for position in range(group_size):
                decoded[position::group_size] = bytes(block[group_size - position - 1::group_size])

            self._update_checksum(decoded)

        self._buffer.extend(data[complete:])
