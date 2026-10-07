"""Validate single-file images and compute their identities."""

import hashlib
import logging
import zlib
from pathlib import Path

from ..errors import SourceError
from ..catalogue.codec import create_empty_entry, is_valid_filename
from .formats.chd import detect_platform
from ..reporting.diagnostics import Diagnostics
from ..io.files import CHUNK, open_regular_file, compute_sha256
from .formats.mega_drive import MegaDriveIdentifier
from .types import Rom
from .platforms import EXTENSIONS


logger = logging.getLogger(__name__)


def inspect_rom(
    path: Path, name: str, *, diagnostics: Diagnostics | None = None, diagnostic_path: Path | None = None,
    md_identifier: MegaDriveIdentifier | None = None,
) -> Rom:
    diagnostics = diagnostics or Diagnostics()
    diagnostic_path = diagnostic_path if diagnostic_path is not None else Path(name)

    if not is_valid_filename(name):
        raise SourceError("Non-portable ROM filename", path=diagnostic_path)

    suffix = Path(name).suffix.lower()

    if suffix == ".chd":
        try:
            platform = detect_platform(path)

        except SourceError as exc:
            raise SourceError(exc.message, path=diagnostic_path) from exc

        # Container hashes identify the copied file, not Redump tracks. Do not pass
        # CHD header/raw hashes to cartridge metadata providers as game identities.
        return Rom(path, create_empty_entry(name, compute_sha256(path), platform), [])

    size = path.stat().st_size

    with open_regular_file(path) as stream:
        header = stream.read(512)

    platform = EXTENSIONS.get(suffix)
    needs_md_identity = suffix == ".bin" and header[256:260] != b"SEGA"

    if suffix == ".bin":

        if needs_md_identity and md_identifier is None:
            raise SourceError("Ambiguous .bin (not an identified Mega Drive cartridge)", path=diagnostic_path)

        platform = "MD"

    if platform is None:
        raise SourceError("Unsupported ROM", path=diagnostic_path)

    offset = 0
    payload_end = size

    if suffix == ".nes":

        if not header.startswith(b"NES\x1a") or size <= 16:
            raise SourceError(
                f"Invalid iNES header (size={size}, magic={header[:4].hex()})", path=diagnostic_path,
            )

        offset = 16 + (512 if header[6] & 4 else 0)
        prg, chr_size = header[4] * 16384, header[5] * 8192

        if header[7] & 12 == 8:

            if header[9] & 15 == 15:
                prg = (1 << (header[4] >> 2)) * ((header[4] & 3) * 2 + 1)

            else:
                prg += (header[9] & 15) * 256 * 16384

            if header[9] >> 4 == 15:
                chr_size = (1 << (header[5] >> 2)) * ((header[5] & 3) * 2 + 1)

            else:
                chr_size += (header[9] >> 4) * 256 * 8192

        payload_end = offset + prg + chr_size

        if prg == 0:
            raise SourceError("Empty NES PRG-ROM", path=diagnostic_path)

        if size < payload_end:
            raise SourceError(
                f"NES header/data size mismatch: header requires {payload_end} bytes, got {size}",
                path=diagnostic_path,
            )

    full, raw, payload = hashlib.sha256(), hashlib.sha1(), hashlib.sha1()
    raw_crc = payload_crc = 0
    position = 0
    nonzero_tail = False

    with open_regular_file(path) as stream:

        while chunk := stream.read(CHUNK):
            full.update(chunk)
            raw.update(chunk)
            payload_chunk = chunk[max(0, offset - position) : max(0, payload_end - position)]
            tail = chunk[max(0, payload_end - position) :]
            nonzero_tail = nonzero_tail or tail.count(0) != len(tail)
            payload.update(payload_chunk)
            raw_crc = zlib.crc32(chunk, raw_crc)
            payload_crc = zlib.crc32(payload_chunk, payload_crc)
            position += len(chunk)

    if needs_md_identity:

        if not md_identifier.is_known_rom(sha1=raw.hexdigest(), size=size, crc32=f"{raw_crc:08x}"):
            raise SourceError(
                "Ambiguous .bin (no Mega Drive signature or verified DAT identity)", path=diagnostic_path,
            )

        diagnostics.emit(
            "identity", "verified_mega_drive", name=name, sha256=full.hexdigest(), sha1=raw.hexdigest(),
            size=size, crc32=f"{raw_crc:08x}", database_url=md_identifier.url,
            basis="full_file_sha1_size_crc32",
        )
        logger.info("%s — Mega Drive verified by DAT (full SHA-1, size and CRC)", diagnostic_path)

    lookups = [(raw.hexdigest(), size)]
    crc32s = {lookups[0]: f"{raw_crc:08x}"}
    trailing = size - payload_end
    # Extra data can identify a distinct dump. Prefer its full hash, and do not
    # strip meaningful extra ROM blocks from extended-console lookup identities.

    if offset and not (trailing and header[7] & 15):
        payload_lookup = (payload.hexdigest(), payload_end - offset)
        lookups.insert(len(lookups) if trailing else 0, payload_lookup)
        crc32s[payload_lookup] = f"{payload_crc:08x}"

    if trailing:
        outcome = "nes_trailing_data" if nonzero_tail or header[7] & 15 else "nes_zero_padding"
        diagnostics.emit("source", outcome, name=name, bytes=trailing, flags7=header[7], nonzero=nonzero_tail)
        logger.warning(
            "%s — kept %d trailing bytes (flags7=0x%02x, nonzero=%s); full-file lookup first",
            diagnostic_path, trailing, header[7], nonzero_tail,
        )

    return Rom(
        path, create_empty_entry(name, full.hexdigest(), platform), lookups,
        crc32s=crc32s, full_lookup=(raw.hexdigest(), size),
    )
