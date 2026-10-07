"""Persistent, content-addressed PC datasets with validated atomic updates."""

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from ..errors import CatalogueError
from ..reporting.diagnostics import Diagnostics
from ..io.files import write_atomic, ensure_directory, read_bytes, compute_sha256
from ..io.http import NetworkError
from ..io import paths


logger = logging.getLogger(__name__)


class DatasetCache:
    def __init__(
        self, root: Path, *, offline: bool = False, refresh: bool = False, diagnostics: Diagnostics | None = None
    ) -> None:
        self.root = root
        self.offline = offline
        self.refresh = refresh
        self.lock = threading.Lock()
        self.paths: dict[tuple[str, str], Path | None] = {}
        self.diagnostics = diagnostics or Diagnostics()

    def obtain(
        self,
        name: str,
        url: str,
        *,
        download: Callable[[], bytes | None],
        prepare: Callable[[bytes], bytes],
        validate: Callable[[Path], None],
        limit: int,
    ) -> Path | None:

        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", name):
            raise ValueError("Invalid dataset name")

        # Preparation is once per run, even if many ROM workers request the same database.

        with self.lock:
            identity = (name, url)

            if identity in self.paths:

                if self.paths[identity] is None:
                    self.diagnostics.emit("issue", "dataset_unavailable", dataset=name)

                return self.paths[identity]

            self.paths[identity] = None
            directory = self.root / name
            ensure_directory(directory)
            manifest = directory / paths.DATASET_MANIFEST_FILE
            previous = None

            if manifest.exists():
                try:
                    record = json.loads(read_bytes(manifest, 4096))
                    digest = record["sha256"]

                    if record["url"] != url or not re.fullmatch(r"[a-f0-9]{64}", digest):
                        raise ValueError("Invalid dataset identity")

                    previous = directory / digest

                    if previous.is_symlink() or previous.stat().st_size > limit or compute_sha256(previous) != digest:
                        raise ValueError("Dataset checksum mismatch")

                    validate(previous)

                except (OSError, ValueError, KeyError, TypeError):
                    logger.warning("%s — invalid dataset cache; ignored", Path(name))
                    self.diagnostics.emit("issue", "invalid_dataset_cache", dataset=name)
                    previous = None

            if previous and (not self.refresh or self.offline):
                self.diagnostics.emit("dataset", "cache_hit", dataset=name, sha256=previous.name)
                self.paths[identity] = previous
                return previous

            if self.offline:
                self.diagnostics.emit("issue", "dataset_offline_miss", dataset=name)
                return None

            try:
                logger.info("%s — downloading dataset", Path(name))
                raw = download()

                if raw is None:
                    raise NetworkError("Dataset not found")

                data = prepare(raw)

                if not data or len(data) > limit:
                    raise CatalogueError("Dataset exceeds size limit or is empty")

                ensure_directory(directory)
                digest = hashlib.sha256(data).hexdigest()
                path = directory / digest
                # This blob is unreferenced until validated; current.json still refers to the old version.
                write_atomic(path, data)
                validate(path)
                write_atomic(manifest, (json.dumps({"url": url, "sha256": digest}) + "\n").encode())
                self.paths[identity] = path
                self.diagnostics.emit("dataset", "downloaded", dataset=name, sha256=digest)
                return path

            except (OSError, ValueError, NetworkError) as exc:
                reason = exc.reason if isinstance(exc, NetworkError) else type(exc).__name__
                status = exc.status if isinstance(exc, NetworkError) else None
                self.diagnostics.emit("issue", "dataset_update_failed", dataset=name, reason=reason, status=status)
                logger.warning("%s — dataset update failed (%s, HTTP status=%s)", Path(name), reason, status)

                if previous:
                    logger.warning("%s — previous verified dataset retained", Path(name))
                    self.paths[identity] = previous
                    return previous

                return None


# SQLite connections are acquired per lookup/thread, never shared or opened by constructors.
@contextmanager
def read_database(path: Path) -> Iterator[sqlite3.Connection]:
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)

    except sqlite3.Error as exc:
        raise ValueError("Unable to open metadata database") from exc

    try:
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA query_only=ON")
        deadline = time.monotonic() + 5

        def is_deadline_expired() -> int:
            return int(time.monotonic() > deadline)

        connection.set_progress_handler(is_deadline_expired, 10000)
        connection.row_factory = sqlite3.Row
        yield connection

    except sqlite3.Error as exc:
        raise ValueError("Invalid or unresponsive metadata database") from exc

    finally:
        connection.close()
