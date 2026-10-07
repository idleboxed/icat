"""Thread-safe, credential-free run diagnostics, independent of logging handlers.

SPDX-License-Identifier: BSD-3-Clause
"""

import copy
import threading
from collections import Counter, deque
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal


def get_iso_time() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class NetworkActivity:
    host: str
    state: Literal["request", "rate_limit", "pacing", "slot"]
    until: float | None = None


def _summarize(groups: dict[tuple, dict], identity: tuple, event: dict) -> int:

    if identity not in groups:

        if len(groups) >= 256:
            return 1

        groups[identity] = {"first": event, "count": 0}

    group = groups[identity]

    if group["count"]:
        group["last"] = event

    group["count"] += 1
    return 0


def get_network_failure_reason(event: dict) -> str | None:
    status = event.get("status")

    if event["kind"] == "http" and (
        event["outcome"] in {"failure", "auth_blocked", "wait_budget_exhausted"}
        or (event["outcome"] == "response" and status is not None and status >= 400 and status != 404)
    ):
        return event.get("reason") or {
            "auth_blocked": "auth", "wait_budget_exhausted": "wait_budget",
        }.get(event["outcome"], "http_status" if status is not None else event["outcome"])

    return None


class Diagnostics:
    def __init__(self, *, event_limit: int = 10000) -> None:

        if event_limit < 0:
            raise ValueError("Event limit must not be negative")

        self.lock = threading.Lock()
        self.context = ContextVar("icat_diagnostics", default=None)
        self.event_limit = event_limit
        self.events: list[dict] = []
        self.tail: deque[dict] = deque(maxlen=min(1000, event_limit // 2))
        self.head_limit = event_limit - self.tail.maxlen
        self.counts: dict[str, Counter] = {}
        self.hosts: dict[str, Counter] = {}
        self.issues: dict[tuple, dict] = {}
        self.network_failures: dict[tuple, dict] = {}
        self.issues_omitted = 0
        self.network_failures_omitted = 0
        self.dropped = 0
        self.activities: dict[int, NetworkActivity] = {}

    def reset(self) -> None:

        with self.lock:
            self.events.clear()
            self.tail.clear()
            self.counts.clear()
            self.hosts.clear()
            self.issues.clear()
            self.network_failures.clear()
            self.issues_omitted = 0
            self.network_failures_omitted = 0
            self.dropped = 0
            self.activities.clear()

    @contextmanager
    def track_activity(self, activity: NetworkActivity) -> Iterator[None]:
        """Transient per-thread gauges, separate from durable diagnostic history."""
        thread = threading.get_ident()

        with self.lock:
            previous = self.activities.get(thread)
            self.activities[thread] = activity

        try:
            yield

        finally:

            with self.lock:

                if previous is None:
                    self.activities.pop(thread, None)

                else:
                    self.activities[thread] = previous

    def get_activity_snapshot(self) -> tuple[NetworkActivity, ...]:

        with self.lock:
            return tuple(self.activities.values())

    @contextmanager
    def capture_events(self, source: str, rom: str | None) -> Iterator[list[dict]]:
        issues: list[dict] = []
        token = self.context.set((source, rom, issues))

        try:
            yield issues

        finally:
            self.context.reset(token)

    def emit(self, kind: str, outcome: str, **details: object) -> None:
        context = self.context.get()
        event = {"time": get_iso_time(), "kind": kind, "outcome": outcome, **details}
        duplicate_failure = False

        if context:
            source, rom, issues = context
            event.update(source=source)

            if rom is not None:
                event["rom"] = rom

            # Source wrappers still need the issue in their local scope to mark
            # metadata partial. Its transport cause is already stored once below.
            duplicate_failure = kind == "issue" and outcome == "source_error" and any(
                (reason := get_network_failure_reason(previous)) is not None and (
                    reason == details.get("reason")
                    or (details.get("status") is not None and previous.get("status") == details["status"])
                ) for previous in issues
            )
            issues.append(event)

        with self.lock:
            bucket = event.get("source", "transport")
            self.counts.setdefault(bucket, Counter())[f"{kind}.{outcome}"] += 1

            if kind == "issue" and not duplicate_failure:
                identity = (
                    bucket, outcome, details.get("reason"), details.get("status"),
                    details.get("dataset"), details.get("field"),
                    details.get("genre") if outcome == "unknown_genre" else None,
                )
                self.issues_omitted += _summarize(self.issues, identity, event)

            status = event.get("status")

            if (reason := get_network_failure_reason(event)) is not None:
                # Neither ROM nor request identity belongs in a failure group:
                # a collection-wide outage must not consume the diagnostic budget.
                identity = (bucket, event.get("host"), reason, status)
                self.network_failures_omitted += _summarize(self.network_failures, identity, event)

                if identity in self.network_failures:
                    self.network_failures[identity].update(
                        source=bucket, host=event.get("host"), reason=reason, status=status,
                    )

            if event.get("host"):
                counts = self.hosts.setdefault(event["host"], Counter())
                counts[f"{kind}.{outcome}"] += 1

                if event.get("sent"):
                    counts["requests_sent"] += 1

                    if event.get("status") is not None:
                        counts[f"HTTP_{event["status"]}"] += 1

            # Routine lookups, HTTP/cache hits, missing keys, waits and thumbnail
            # misses are useful as totals, not thousands of per-ROM records.
            # Issues and transport failures have their own bounded summaries.

            if kind not in {"source", "identity", "dataset"}:
                return

            if len(self.events) < self.head_limit:
                self.events.append(event)

            else:

                if len(self.tail) == self.tail.maxlen:
                    self.dropped += 1

                self.tail.append(event)

    def get_snapshot(self) -> dict:

        with self.lock:
            return {
                "counts": {name: dict(counts) for name, counts in self.counts.items()},
                "hosts": {name: dict(counts) for name, counts in self.hosts.items()},
                "events": copy.deepcopy(self.events + list(self.tail)),
                "events_prefix_count": len(self.events),
                "events_tail_count": len(self.tail),
                "events_omitted": self.dropped,
                "issues": copy.deepcopy(list(self.issues.values())),
                "issues_omitted": self.issues_omitted,
                "network_failures": copy.deepcopy(list(self.network_failures.values())),
                "network_failures_omitted": self.network_failures_omitted,
            }
