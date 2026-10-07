"""Presentation-neutral run progress carried by standard logging records.

SPDX-License-Identifier: BSD-3-Clause
"""

import logging
from dataclasses import dataclass, replace
from time import monotonic
from types import TracebackType
from typing import Literal


logger = logging.getLogger(__name__)
StepStatus = Literal["pending", "running", "done", "not_needed", "failed", "interrupted"]
RunStatus = Literal["running", "complete", "complete_with_rejections", "failed", "interrupted"]


@dataclass(frozen=True)
class Step:
    key: str
    title: str
    unit: str = "actions"


@dataclass(frozen=True)
class StepProgress:
    step: Step
    status: StepStatus = "pending"
    completed: int = 0
    total: int | None = None
    item: str | None = None

    @property
    def fraction(self) -> float | None:

        if self.status in {"done", "not_needed"}:
            return 1.0

        return self.completed / self.total if self.total else None

    @property
    def percent(self) -> int | None:
        fraction = self.fraction
        return int(fraction * 100) if fraction is not None else None


@dataclass(frozen=True)
class RunInventory:
    source_files: int | None = None
    candidate_sources: int | None = None
    catalogue_entries: int | None = None
    prepared_games: int = 0
    duplicate_sources: int = 0
    rejected_sources: int = 0
    skipped_sources: int = 0


@dataclass(frozen=True)
class ProviderProgress:
    source: str
    index: int
    sources: int
    completed: int
    total: int
    status: str = "running"

    @property
    def percent(self) -> int | None:
        return int(100 * self.completed / self.total) if self.total else None

    def emit(self) -> None:
        logger.info(
            "Metadata source %s (%d/%d): %d/%d games checked; %s",
            self.source, self.index, self.sources, self.completed, self.total, self.status,
            extra={"icat_provider_progress": self},
        )


@dataclass(frozen=True)
class ProgressSnapshot:
    steps: tuple[StepProgress, ...]
    index: int
    status: RunStatus
    started: float
    updated: float
    inventory: RunInventory = RunInventory()

    @property
    def current(self) -> StepProgress:
        return self.steps[self.index]

    @property
    def resolved(self) -> int:
        return sum(step.status in {"done", "not_needed"} for step in self.steps)

    @property
    def percent(self) -> int:

        if self.status in {"complete", "complete_with_rejections"}:
            return 100

        fraction = sum(step.fraction or 0 for step in self.steps)
        # A fully counted final action is not proof that context cleanup succeeded.
        return min(99, int(fraction * 100 / len(self.steps)))


class RunProgress:
    """One sequential plan; counters never perform I/O or control work scheduling."""

    def __init__(self, steps: tuple[Step, ...]) -> None:

        if not steps or len({step.key for step in steps}) != len(steps):
            raise ValueError("Progress plan must contain distinct steps")

        self.steps = tuple(StepProgress(step) for step in steps)
        self.index = -1
        self.started = 0.0
        self.status: RunStatus = "running"
        self.inventory = RunInventory()
        self.rejected = False

    def __enter__(self) -> "RunProgress":
        self.started = monotonic()
        return self

    def start(self, key: str, *, total: int | None = None) -> None:
        next_index = self.index + 1

        if next_index >= len(self.steps) or self.steps[next_index].step.key != key:
            raise ValueError("Progress steps must follow the run plan")

        if total is not None and total < 0:
            raise ValueError("Progress total must not be negative")

        if self.index >= 0:
            self._finish_step()

        self.index = next_index
        self._update(status="running", total=total)

    def set_total(self, total: int) -> None:
        current = self.steps[self.index]

        if current.total is not None or current.completed or total < 0:
            raise ValueError("Progress total can only be set before counted work")

        self._update(total=total)

    def set_item(self, value: str) -> None:
        self._update(item=value)

    def set_counts(
        self, *, source_files: int | None = None, candidate_sources: int | None = None,
        catalogue_entries: int | None = None, prepared_games: int | None = None,
        duplicate_sources: int | None = None, rejected_sources: int | None = None,
        skipped_sources: int | None = None,
    ) -> None:
        changes = {
            key: value for key, value in (
                ("source_files", source_files), ("candidate_sources", candidate_sources),
                ("catalogue_entries", catalogue_entries),
                ("prepared_games", prepared_games), ("duplicate_sources", duplicate_sources),
                ("rejected_sources", rejected_sources), ("skipped_sources", skipped_sources),
            ) if value is not None
        }

        if any(value < 0 for value in changes.values()):
            raise ValueError("Inventory counts must not be negative")

        self.inventory = replace(self.inventory, **changes)
        self._update()

    def advance(self) -> None:
        current = self.steps[self.index]

        if current.total is None or current.completed >= current.total:
            raise ValueError("Progress advance exceeds the known total")

        self._update(completed=current.completed + 1, item=None)

    def _finish_step(self) -> None:
        current = self.steps[self.index]

        if current.total is not None and current.completed != current.total:
            raise ValueError("Progress step has unfinished counted work")

        self._update(status="not_needed" if current.total == 0 else "done", item=None)

    def _update(self, **changes: object) -> None:
        updated = replace(self.steps[self.index], **changes)
        self.steps = self.steps[:self.index] + (updated,) + self.steps[self.index + 1:]
        snapshot = ProgressSnapshot(self.steps, self.index, self.status, self.started, monotonic(), self.inventory)
        logger.info(
            "%s: %s (%d/%s %s)", updated.step.title, updated.status,
            updated.completed, updated.total if updated.total is not None else "?", updated.step.unit,
            extra={"icat_progress": snapshot},
        )

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None,
    ) -> None:

        if exc is not None:
            self.status = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"

            if self.index >= 0:
                self._update(status=self.status)

            return

        if self.index != len(self.steps) - 1:
            raise ValueError("Progress run has unfinished steps")

        self._finish_step()
        self.status = "complete_with_rejections" if self.rejected else "complete"
        self._update()
