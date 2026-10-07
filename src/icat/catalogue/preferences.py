"""Explicit trash preferences access; ordinary imports do not read or write prefs.

SPDX-License-Identifier: BSD-3-Clause
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from ..errors import CatalogueError
from ..io.files import write_atomic, read_bytes, resolve_relative_file, sync_directory
from ..io import paths


LIMIT = 1024 * 1024


@dataclass
class Preferences:
    root: Path
    original: bytes | None
    document: dict
    trash: frozenset[str]

    @classmethod
    def load(cls, root: Path) -> "Preferences":
        path = resolve_relative_file(root, paths.PREFERENCES_FILE)
        original = read_bytes(path, LIMIT) if path.exists() else None

        try:
            document = json.loads(original) if original is not None else {}

        except (ValueError, UnicodeError) as exc:
            raise CatalogueError("Invalid preferences JSON", path=path) from exc

        if not isinstance(document, dict):
            raise CatalogueError("Preferences must be a JSON object", path=path)

        trash = document.get("trash", [])

        if not isinstance(trash, list) or any(
            not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value) for value in trash
        ):
            raise CatalogueError("Invalid preferences trash: expected an array of SHA-256 hashes", path=path)

        return cls(root, original, document, frozenset(trash))

    def check_unchanged(self) -> None:
        pending = self.root / paths.PREFERENCES_TEMP_FILE

        if pending.exists() or pending.is_symlink():
            raise CatalogueError("Preferences write is pending; inspect before retrying", path=pending)

        path = resolve_relative_file(self.root, paths.PREFERENCES_FILE)
        current = read_bytes(path, LIMIT) if path.exists() else None

        if current != self.original:
            raise CatalogueError(
                "Preferences changed during sync; refusing trash operation", path=self.root / paths.PREFERENCES_FILE,
            )

    def reset_trash(self) -> None:
        self.check_unchanged()

        if not self.trash:
            return

        document = {**self.document, "trash": []}
        encoded = (json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n").encode()

        if len(encoded) > LIMIT:
            raise CatalogueError("Preferences exceed the 1 MiB limit", path=self.root / paths.PREFERENCES_FILE)

        sync_directory(self.root)
        path = resolve_relative_file(self.root, paths.PREFERENCES_FILE)
        self.check_unchanged()
        write_atomic(path, encoded)

        if read_bytes(path, LIMIT) != encoded:
            raise CatalogueError("Preferences readback failed", path=self.root / paths.PREFERENCES_FILE)

        self.original, self.document, self.trash = encoded, document, frozenset()
