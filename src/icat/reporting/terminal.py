"""CLI-only presentation adapters for the shared progress snapshots.

SPDX-License-Identifier: BSD-3-Clause
"""

import logging
import math
import os
from collections.abc import Iterator, Mapping
from copy import copy
from pathlib import Path
from time import monotonic
from typing import TextIO

from rich.console import Console, Group
from rich.live import Live
from rich.progress_bar import ProgressBar
from rich.text import Text

from ..errors import CatalogueError, SourceError
from .diagnostics import Diagnostics, NetworkActivity
from ..io.paths import quote_path
from .progress import ProgressSnapshot, ProviderProgress, RunInventory, StepProgress


LOG_FORMAT = "%(message)s"


class MessageFormatter(logging.Formatter):
    """Render typed paths/errors without parsing messages or changing logging records."""

    def __init__(self, destination: Path | None = None) -> None:
        super().__init__(LOG_FORMAT)
        self.destination = destination.absolute() if destination is not None else None

    def format_filename(self, path: Path) -> str:

        if path.is_absolute():

            if self.destination is not None and path.is_relative_to(self.destination):
                path = path.relative_to(self.destination)

            else:
                path = Path(path.name)

        return quote_path(path)

    def format_error(self, exc: Exception, *, status: str = "", known: tuple[Path, ...] = ()) -> str:
        path = None

        if isinstance(exc, CatalogueError):
            message, path = exc.message, exc.path

            if isinstance(exc, SourceError) and path is not None:

                if any(path == item or (not path.is_absolute() and path.name == item.name) for item in known):
                    path = None

                elif not path.is_absolute():
                    path = Path(path.name)

        elif isinstance(exc, OSError) and exc.filename is not None:
            message = exc.strerror or type(exc).__name__
            path = Path(os.fsdecode(exc.filename))

            if exc.filename2 is not None:
                message = f"{message} -> {self.format_filename(Path(os.fsdecode(exc.filename2)))}"

        else:
            message = f"{exc}"

        message = f"{status}: {message}" if status else message

        if path is not None and path not in known:
            return f"{self.format_filename(path)} — {message}"

        return message

    def format(self, record: logging.LogRecord) -> str:
        rendered = copy(record)

        if isinstance(record.args, tuple):
            known = tuple(arg for arg in record.args if isinstance(arg, Path))
            rendered.args = tuple(
                self.format_filename(arg) if isinstance(arg, Path)
                else self.format_error(arg, known=known) if isinstance(arg, Exception) else arg
                for arg in record.args
            )

        message = super().format(rendered)
        prefix = "ERROR " if record.levelno >= logging.ERROR else "WARN  " if record.levelno >= logging.WARNING else ""
        return f"{prefix}{message}"


def should_use_rich(mode: str, stream: TextIO, environ: Mapping[str, str]) -> bool:

    if mode not in {"auto", "plain", "rich"}:
        raise ValueError("Unknown progress display mode")

    if mode == "plain":
        return False

    return mode == "rich" or (stream.isatty() and environ.get("TERM") not in {"dumb", "unknown"})


def sanitize_text(value: str) -> str:
    """Do not let filenames or provider strings inject terminal control sequences."""
    return "".join(char if char.isprintable() else f"\\x{ord(char):02x}" for char in value)


def format_step_label(step: StepProgress) -> str:

    if step.status == "not_needed" or step.total == 0:
        return "not needed"

    if step.total is None:
        return step.status if step.status != "running" else "working; total unknown"

    percent = f"{step.percent}%" if step.percent is not None else "--"
    return f"{step.completed}/{step.total} {step.step.unit} ({percent}) · {step.status}"


def format_progress_label(snapshot: ProgressSnapshot) -> str:
    return f"[{snapshot.index + 1}/{len(snapshot.steps)}] {snapshot.current.step.title}"


def format_inventory_label(inventory: RunInventory) -> str:
    files, candidates, records = (
        "?" if value is None else f"{value}"
        for value in (inventory.source_files, inventory.candidate_sources, inventory.catalogue_entries)
    )
    return f"Source: {files} files at scan · {candidates} candidates | Catalogue: {records} confirmed records"


def format_provider_label(provider: ProviderProgress) -> str:
    percent = f" ({provider.percent}%)" if provider.percent is not None else ""
    return (
        f"Provider {provider.index}/{provider.sources}: {provider.source} · "
        f"{provider.completed}/{provider.total} games checked{percent} · {provider.status.replace("_", " ")}"
    )


class WarningGroups:
    """Bound identical warning output in RAM, without changing durable diagnostics."""

    def __init__(self) -> None:
        self.repeated: dict[str, tuple[str, int]] = {}

    def accept(self, message: str, level: int, *, identity: str) -> bool:

        if level != logging.WARNING:
            return True

        if identity in self.repeated:
            first, count = self.repeated[identity]
            self.repeated[identity] = first, count + 1
            return False

        if len(self.repeated) < 128:
            self.repeated[identity] = message, 0

        return True

    def drain(self) -> Iterator[str]:

        for message, count in self.repeated.values():

            if count:
                yield f"{message} (repeated {count} more times)"

        self.repeated.clear()


class PlainProgressHandler(logging.StreamHandler):
    """Compact, line-oriented output with throttled current-step/provider counters."""

    def __init__(self, stream: TextIO, *, destination: Path | None = None) -> None:
        super().__init__(stream)
        self.snapshot: ProgressSnapshot | None = None
        self.provider: ProviderProgress | None = None
        self.last_progress = 0.0
        self.last_provider_progress = 0.0
        self.groups = WarningGroups()
        self.setFormatter(MessageFormatter(destination))

    def write_line(self, message: str) -> None:
        self.stream.write(f"{sanitize_text(message)}{self.terminator}")
        self.flush()

    def flush_repeats(self) -> None:

        for message in self.groups.drain():
            self.write_line(message)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.emit_message(record)

        except (OSError, ValueError):
            self.handleError(record)

    def emit_message(self, record: logging.LogRecord) -> None:
        provider = getattr(record, "icat_provider_progress", None)

        if isinstance(provider, ProviderProgress):
            previous = self.provider
            self.provider = provider
            boundary = previous is None or (previous.index, previous.status) != (provider.index, provider.status)
            now = monotonic()

            if not boundary and now - self.last_provider_progress < 5:
                return

            self.last_provider_progress = now
            self.write_line(format_provider_label(provider))
            return

        snapshot = getattr(record, "icat_progress", None)

        if isinstance(snapshot, ProgressSnapshot):
            previous = self.snapshot
            self.snapshot = snapshot

            if previous is None or previous.index != snapshot.index:
                self.provider = None

            if snapshot.status in {"complete", "complete_with_rejections"}:
                return

            inventory = snapshot.inventory
            inventory_values = (inventory.source_files, inventory.candidate_sources, inventory.catalogue_entries)
            inventory_changed = previous is None or (
                previous.inventory.source_files, previous.inventory.candidate_sources,
                previous.inventory.catalogue_entries,
            ) != inventory_values
            boundary = previous is None or (previous.index, previous.status) != (snapshot.index, snapshot.status)
            now = monotonic()
            show_step = boundary or now - self.last_progress >= 5

            if self.provider is not None and snapshot.current.step.key == "metadata" and not boundary:
                show_step = False

            if show_step:
                self.last_progress = now
                self.write_line(f"{format_progress_label(snapshot)} · {format_step_label(snapshot.current)}")

            if inventory_changed and all(value is not None for value in inventory_values):
                self.write_line(format_inventory_label(inventory))

            if not show_step:
                return

            if snapshot.current.item:
                self.write_line(snapshot.current.item)

            return

        if getattr(record, "icat_summary", None):
            self.flush_repeats()

        message = self.format(record)

        if self.groups.accept(message, record.levelno, identity=record.getMessage()):
            self.write_line(message)


class RichProgressHandler(logging.Handler):
    """One current-operation panel; ticks read RAM, never the run journal."""

    def __init__(self, stream: TextIO, diagnostics: Diagnostics, *, destination: Path | None = None) -> None:
        super().__init__()
        self.diagnostics = diagnostics
        self.snapshot: ProgressSnapshot | None = None
        self.provider: ProviderProgress | None = None
        self.last_issue: str | None = None
        self.journal: str | None = None
        self.mode = ""
        self.groups = WarningGroups()
        self.console = Console(file=stream, force_terminal=True, markup=False, highlight=False)
        self.live = Live(
            console=self.console, get_renderable=self.render, refresh_per_second=4,
            redirect_stdout=False, redirect_stderr=False,
        )
        self.setFormatter(MessageFormatter(destination))

    def flush_repeats(self) -> None:

        for message in self.groups.drain():
            self.console.print(Text(sanitize_text(message), style="yellow"))

    def emit(self, record: logging.LogRecord) -> None:
        snapshot = getattr(record, "icat_progress", None)

        if isinstance(snapshot, ProgressSnapshot):

            if self.snapshot is None or self.snapshot.index != snapshot.index:
                self.provider = None

            self.snapshot = snapshot
            return

        provider = getattr(record, "icat_provider_progress", None)

        if isinstance(provider, ProviderProgress):
            self.provider = provider
            return

        if journal := getattr(record, "icat_journal", None):
            self.journal = journal

        if mode := getattr(record, "icat_mode", None):
            self.mode = mode

        try:

            if getattr(record, "icat_summary", None):
                self.flush_repeats()

            message = self.format(record)

            if record.levelno >= logging.WARNING:
                self.last_issue = message

            if not self.groups.accept(message, record.levelno, identity=record.getMessage()):
                return

            style = "red" if record.levelno >= logging.ERROR else "yellow" if record.levelno >= logging.WARNING else ""

            if summary := getattr(record, "icat_summary", None):
                style = "bold yellow" if summary == "COMPLETED WITH REJECTIONS" else "bold"

            self.console.print(Text(sanitize_text(message), style=style))

        except (OSError, ValueError):
            self.handleError(record)

    def render(self) -> Group:
        snapshot = self.snapshot

        if snapshot is None or snapshot.status in {"complete", "complete_with_rejections"}:
            return Group()

        current, inventory = snapshot.current, snapshot.inventory
        now = monotonic() if snapshot.status == "running" else snapshot.updated
        elapsed = max(0, int(now - snapshot.started))
        rows = [
            Text(f"ICAT / {self.mode or "SYNC"} · {elapsed // 60:02}:{elapsed % 60:02} elapsed", style="bold"),
            Text(sanitize_text(format_progress_label(snapshot)), style="cyan"),
        ]
        provider = self.provider if current.step.key == "metadata" else None
        rows.append(Text(sanitize_text(
            format_provider_label(provider) if provider is not None else format_step_label(current)
        )))
        percent = provider.percent if provider is not None else current.percent

        if snapshot.status == "running":
            rows.append(ProgressBar(total=100, completed=percent or 0, pulse=percent is None))

        if current.item:
            rows.append(Text(sanitize_text(current.item), overflow="ellipsis", no_wrap=True))

        if snapshot.status == "running":
            activities = self.diagnostics.get_activity_snapshot()
            requests = sum(activity.state == "request" for activity in activities)
            waits = [activity for activity in activities if activity.state != "request"]

            if activities:
                rows.append(Text(f"HTTP: {requests} active · {len(waits)} waiting"))

            if waits:
                def wait_deadline(activity: NetworkActivity) -> float:
                    return activity.until or 0

                wait = max(waits, key=wait_deadline)
                delay = f" · {max(0, math.ceil(wait.until - now))}s remaining" if wait.until is not None else ""
                reason = {
                    "rate_limit": "host rate limit", "pacing": "request spacing", "slot": "free HTTP slot",
                }.get(wait.state, wait.state)
                rows.append(Text(sanitize_text(f"Waiting for {reason}: {wait.host}{delay}"), style="yellow"))

        rows.append(Text(format_inventory_label(inventory)))

        if self.mode in {"", "COPY", "MOVE"}:
            rows.append(Text(
                f"Prepared: {inventory.prepared_games} new games · {inventory.duplicate_sources} duplicate sources"
            ))
            rows.append(Text(
                f"Rejected: {inventory.rejected_sources} · skipped: {inventory.skipped_sources} · these sources kept"
            ))

        if self.last_issue:
            rows.append(Text(sanitize_text(f"Last issue: {self.last_issue}"), overflow="ellipsis", no_wrap=True))

        if self.journal:
            rows.append(Text(f"{sanitize_text(quote_path(Path(self.journal).name))} — run journal", overflow="fold"))

        rows.append(Text(
            "Ctrl+C: request cancellation; no rollback" if snapshot.status == "running"
            else f"Run: {snapshot.status}; completed writes are not rolled back"
        ))
        return Group(*rows)
