"""Durable operation checkpoints in sortable UTC run directories.

SPDX-License-Identifier: BSD-3-Clause
"""

import json
import logging
import os
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, time
from types import TracebackType
from typing import BinaryIO
from urllib.parse import urlsplit, urlunsplit

from .. import __version__
from ..errors import CatalogueError
from .diagnostics import Diagnostics, get_iso_time
from ..io.files import write_atomic, sync_directory
from ..io import paths


logger = logging.getLogger(__name__)
CHECKPOINT_SECONDS = 5 * 60


def _format_filename_timestamp(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class RunJournal:
    def __init__(self, directory: Path, config: dict, diagnostics: Diagnostics) -> None:
        self.directory, self.diagnostics = directory, diagnostics
        self.started = monotonic()
        self.path: Path | None = None
        self.progress_path: Path | None = None
        self.removal_path: Path | None = None
        self._progress: BinaryIO | None = None
        self._removals: BinaryIO | None = None
        self._removal_sequence = 0
        self._removal_failed = False
        self._last_checkpoint: float | None = None
        self.urls: dict[str, str] = {}
        self.data = {
            "version": 4, "icat_version": __version__, "config": config, "state": "prepared",
            "sources": [], "rejected_sources": [], "skipped_sources": [], "removed": [],
            "trash": [], "trash_removed": [], "trash_reset": False,
            "excluded_games": [], "excluded_removed": [],  # Retained as empty legacy report fields.
            "nes_header_fixes": [],
            "metadata_sources": {}, "metadata_fields": {}, "metadata_identifiers": {},
            "source_urls": {}, "summary": {},
        }

    def __enter__(self) -> "RunJournal":
        started = get_iso_time()
        stamp = _format_filename_timestamp(int(time()))
        collision = 0

        while True:
            suffix = f"_{collision:02d}" if collision else ""
            run_directory = self.directory / f"{stamp}{suffix}"

            try:
                # Never reuse a previous run, including two starts within one clock tick.
                run_directory.mkdir(mode=0o700)
                break

            except FileExistsError:
                collision += 1

        sync_directory(self.directory)
        self.path = run_directory / paths.JOURNAL_FILE
        self.data["started_at"] = started
        self.record("prepared", force=True)
        logger.info("%s — run journal", self.path, extra={"icat_journal": f"{self.path}"})
        return self

    def _append_progress(self, event: dict) -> None:

        if self._progress is None:
            self.progress_path = self.path.with_name(paths.PROGRESS_JOURNAL_FILE)
            self._progress = self.progress_path.open("xb", buffering=0)
            self.data["progress_log"] = self.progress_path.name
            sync_directory(self.path.parent)

        data = memoryview((json.dumps(event, separators=(",", ":")) + "\n").encode())

        while data:
            written = self._progress.write(data)

            if not written:
                raise OSError("Unable to append the progress journal")

            data = data[written:]

        os.fsync(self._progress.fileno())

    def record(self, state: str, *, force: bool = False) -> None:
        now = monotonic()
        previous = self.data["state"]
        updated_at = get_iso_time()
        duration = round(now - self.started, 6)
        self.data.update(state=state, updated_at=updated_at, duration_seconds=duration)

        if not force:
            same_stage = previous == state

            if same_stage and self._last_checkpoint is not None and now - self._last_checkpoint < CHECKPOINT_SECONDS:
                return

            event = {"time": updated_at, "state": state, "duration_seconds": duration}

            for name in ("metadata_completed", "current_source", "current_image"):

                if name in self.data:
                    event[name] = self.data[name]

            self._append_progress(event)
            self._last_checkpoint = now
            return

        self.data["diagnostics"] = self.diagnostics.get_snapshot()
        write_atomic(self.path, (json.dumps(self.data, indent=2) + "\n").encode())
        self._last_checkpoint = now

    def _append_removal(self, event: dict) -> None:
        data = memoryview((json.dumps(event, separators=(",", ":")) + "\n").encode())

        while data:
            written = self._removals.write(data)

            if not written:
                raise OSError("Unable to append the removal journal")

            data = data[written:]

        os.fsync(self._removals.fileno())

    @contextmanager
    def track_removal(self, path: Path, *, collection: str, digest: str) -> Iterator[None]:
        """Sync intent before unlink and completion after the caller's directory fsync."""
        states = {
            "removed": "removing_sources", "trash_removed": "removing_trash",
        }

        if collection not in states:
            raise ValueError("Unknown removal collection")

        if self._removal_failed or "pending_removal" in self.data:
            raise CatalogueError("Unresolved removal; refusing further removals")

        self._removal_failed = True
        created = self._removals is None

        if created:
            self.removal_path = self.path.with_name(paths.REMOVAL_JOURNAL_FILE)
            # An unbuffered stream cannot silently retry a failed write on close.
            self._removals = self.removal_path.open("xb", buffering=0)
            sync_directory(self.path.parent)
            self.data["removal_log"] = self.removal_path.name

        self._removal_sequence += 1
        intent = {
            "sequence": self._removal_sequence, "collection": collection,
            "path": f"{path}", "sha256": digest,
        }
        self.data["pending_removal"] = intent
        state = states[collection]

        if created or self.data["state"] != state:
            self.record(state, force=True)

        self._append_removal({**intent, "time": get_iso_time(), "phase": "intent"})
        yield
        self._append_removal({**intent, "time": get_iso_time(), "phase": "complete"})
        self.data[collection].append(f"{path}")
        self.data.pop("pending_removal")
        self._removal_failed = False

    def register_source_refs(self, urls: Sequence[str]) -> list[str]:
        result = []

        for url in urls:
            # Source provenance does not require query strings; some APIs put keys there.
            parsed = urlsplit(url)
            safe = urlunsplit((parsed.scheme, parsed.hostname or "", parsed.path, "", ""))

            if safe not in self.urls:
                ref = f"s{len(self.urls) + 1}"
                self.urls[safe] = ref
                self.data["source_urls"][ref] = safe

            if self.urls[safe] not in result:
                result.append(self.urls[safe])

        return result

    def record_metadata_changes(
        self, digest: str, fields: dict[str, str], urls: Sequence[str] = (), *,
        identifiers: Mapping[str, str] | None = None,
    ) -> None:
        """Keep provenance for changes, not repeated lookup matches."""

        if fields:
            self.data["metadata_fields"].setdefault(digest, {}).update(fields)

        if urls:
            refs = self.data["metadata_sources"].setdefault(digest, [])
            refs.extend(ref for ref in self.register_source_refs(urls) if ref not in refs)

        if identifiers and (fields or urls):
            # These are hash-verified mappings, not IDs parsed from arbitrary URLs.
            # Persist only the known numeric pair; names, tokens and extra IDs stay out.
            names = ("TheGamesDb:game", "TheGamesDb:platform")
            values = [identifiers.get(name) for name in names]

            if all(
                isinstance(value, str) and re.fullmatch(r"[0-9]{1,12}", value) and int(value) > 0
                for value in values
            ):
                self.data["metadata_identifiers"][digest] = {
                    name: f"{int(value)}" for name, value in zip(names, values, strict=True)
                }

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        unresolved = exc is None and self._removal_failed

        if unresolved:
            exc = CatalogueError("Unresolved removal; operation cannot be completed")
            exc_type = type(exc)

        compact = exc is None and self.data["state"] in {"complete", "complete_with_rejections"}

        try:
            self.data["finished_at"] = get_iso_time()

            if exc is None:

                if compact:
                    self.data.pop("removal_log", None)

                self.record(self.data["state"], force=True)

            else:
                self.data["failed_stage"] = self.data["state"]
                self.data["error"] = {"type": exc_type.__name__}

                if isinstance(exc, CatalogueError):
                    self.data["error"]["reason"] = f"{exc}"

                elif isinstance(exc, OSError):
                    self.data["error"]["errno"] = exc.errno

                try:
                    self.record("interrupted" if isinstance(exc, KeyboardInterrupt) else "failed", force=True)

                except OSError:
                    # Do not replace the original failure or delete its recovery log.
                    logger.error("%s — final checkpoint failed", self.path)

        finally:

            if self._progress is not None:
                self._progress.close()

            if self._removals is not None:
                self._removals.close()

        if unresolved:
            raise exc

        if compact and self.removal_path is not None:
            # Only the durable final JSON supersedes the small append-only log.
            self.removal_path.unlink()
            sync_directory(self.path.parent)
