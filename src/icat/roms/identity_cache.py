"""PC-side lookup identities keyed by the catalogue hash, not proof of current ROM contents."""

import json
import logging
import re
from copy import deepcopy
from pathlib import Path

from ..errors import CatalogueError
from ..io.files import ensure_directory, get_file_stamp, read_bytes, resolve_relative_file, write_atomic
from .platforms import EXTENSIONS, PLATFORM_EXTENSIONS
from .types import Rom

logger = logging.getLogger(__name__)
LIMIT = 16 * 1024 * 1024

def _is_valid_digest(value: object, length: int) -> bool:
    return isinstance(value, str) and re.fullmatch(f"[0-9a-f]{{{length}}}", value) is not None

def _is_valid_size(value: object) -> bool:
    return type(value) is int and 0 < value < 2**63

def _is_valid_lookup(value: object) -> bool:
    return (
        isinstance(value, (list, tuple)) and len(value) == 2
        and _is_valid_digest(value[0], 40) and _is_valid_size(value[1])
    )

def _is_valid_record(key: object, record: object) -> bool:

    if not isinstance(key, str) or not _is_valid_digest(key[:64], 64):
        return False

    suffix = key[64:]

    if suffix not in EXTENSIONS or not isinstance(record, dict):
        return False

    if set(record) != {"platform", "size", "lookups", "full_lookup"}:
        return False

    platform, size = record["platform"], record["size"]

    if not isinstance(platform, str) or suffix not in PLATFORM_EXTENSIONS.get(platform, ()):
        return False

    if not _is_valid_size(size):
        return False

    lookups, full_lookup = record["lookups"], record["full_lookup"]

    if not isinstance(lookups, list) or len(lookups) > 3:
        return False

    identities = set()

    for lookup in lookups:

        if not isinstance(lookup, list) or len(lookup) != 3:
            return False

        identity = lookup[:2]

        if not _is_valid_lookup(identity) or identity[1] > size or not _is_valid_digest(lookup[2], 8):
            return False

        identity = tuple(identity)

        if identity in identities:
            return False

        identities.add(identity)

    if suffix == ".chd":
        return not lookups and full_lookup is None

    return (
        _is_valid_lookup(full_lookup)
        and full_lookup[1] == size and tuple(full_lookup) in identities
    )

def _create_unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}

    for key, value in pairs:

        if key in result:
            raise ValueError("Duplicate identity cache field")

        result[key] = value

    return result

class IdentityCache:
    """Reuse lookup hashes without reading ROM contents or certifying their integrity."""

    FILE_NAME = "rom-identities.json"

    def __init__(self, root: Path) -> None:
        self.root = root
        self._records: dict[str, dict] = {}
        self._loaded = False
        self._dirty = False

    def get(self, path: Path, entry: dict) -> Rom | None:
        """Restore independent lookup data for the expected catalogue hash and platform."""
        key = self._get_key(path, entry)

        if key is None:
            return None

        self._load()
        record = self._records.get(key)

        if record is None or record["platform"] != entry.get("platform"):
            return None

        lookups = [(lookup[0], lookup[1]) for lookup in record["lookups"]]
        crc32s = {identity: lookup[2] for identity, lookup in zip(lookups, record["lookups"], strict=True)}
        full_lookup = record["full_lookup"]
        return Rom(
            path, deepcopy(entry), lookups, crc32s=crc32s,
            full_lookup=tuple(full_lookup) if full_lookup is not None else None, size=record["size"],
        )

    def put(self, rom: Rom) -> None:
        """Remember complete lookup data in memory; perform no filesystem access."""
        name = rom.entry.get("image")

        if not isinstance(name, str):
            return

        # Staging slots have no extension; inspection preserves the logical image name.
        key = self._get_key(Path(name), rom.entry)

        if key is None or not isinstance(rom.lookups, list) or not isinstance(rom.crc32s, dict):
            return

        if not all(_is_valid_lookup(lookup) for lookup in rom.lookups):
            return

        identities = [tuple(lookup) for lookup in rom.lookups]

        if set(rom.crc32s) != set(identities):
            return

        if rom.full_lookup is not None and not _is_valid_lookup(rom.full_lookup):
            return

        record = {
            "platform": rom.entry.get("platform"), "size": rom.size,
            "lookups": [[*identity, rom.crc32s[identity]] for identity in identities],
            "full_lookup": list(rom.full_lookup) if rom.full_lookup is not None else None,
        }

        if not _is_valid_record(key, record):
            return

        if self._records.get(key) != record:
            self._records.pop(key, None)
            self._records[key] = record
            self._dirty = True

    def save(self) -> None:
        """Atomically save changed data, retaining the newest entries within the byte limit."""

        if not self._dirty:
            return

        self._load()

        if not self._dirty:
            return

        fragments = []
        kept = {}
        total_size = 3

        for key, record in reversed(self._records.items()):
            fragment = json.dumps({key: record}, separators=(",", ":"))[1:-1].encode("utf-8")
            required_size = len(fragment) + bool(fragments)

            if total_size + required_size <= LIMIT:
                fragments.append(fragment)
                kept[key] = record
                total_size += required_size

        ensure_directory(self.root)
        path = self._get_path()

        try:
            get_file_stamp(path)

        except FileNotFoundError:
            pass

        write_atomic(path, b"{" + b",".join(reversed(fragments)) + b"}\n")

        if len(kept) != len(self._records):
            logger.warning(
                "ROM identity cache limit reached; dropped %d older identities", len(self._records) - len(kept),
            )

        self._records = dict(reversed(kept.items()))
        self._dirty = False

    def _get_key(self, path: Path, entry: dict) -> str | None:
        digest, suffix = entry.get("hash"), path.suffix.lower()

        if not _is_valid_digest(digest, 64) or suffix not in EXTENSIONS:
            return None

        return f"{digest}{suffix}"

    def _get_path(self) -> Path:

        for parent in self.root.absolute().parents:

            if parent.is_symlink():
                raise CatalogueError("Symlink directory is not allowed", path=parent)

        return resolve_relative_file(self.root, self.FILE_NAME)

    def _load(self) -> None:

        if self._loaded:
            return

        path = self._get_path()
        saved = {}

        try:
            stamp = get_file_stamp(path)

            if stamp[2] > LIMIT:
                logger.warning("%s — identity cache exceeds size limit; ignored", self.FILE_NAME)

            else:
                data = read_bytes(path, LIMIT)

                try:
                    document = json.loads(data, object_pairs_hook=_create_unique_object)

                    if not isinstance(document, dict) or not all(
                        _is_valid_record(key, record) for key, record in document.items()
                    ):
                        raise ValueError("Invalid identity cache schema")

                    saved = document

                except (ValueError, UnicodeError, RecursionError):
                    logger.warning("%s — invalid identity cache; ignored", self.FILE_NAME)

        except FileNotFoundError:
            pass

        updates = self._records
        self._records = saved
        self._dirty = False
        self._loaded = True

        for key, record in updates.items():

            if self._records.get(key) != record:
                self._records.pop(key, None)
                self._records[key] = record
                self._dirty = True
