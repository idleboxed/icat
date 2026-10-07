"""Source template method, common validation, normalization and network/file helpers."""

import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import FIRST_COMPLETED, Executor, Future, wait
from html.parser import HTMLParser
from itertools import batched
from pathlib import Path
from urllib.parse import urlsplit

from ...errors import CatalogueError
from ...catalogue.codec import is_clean_text, validate
from ...databases.cache import DatasetCache
from ..genres import resolve_genre
from ...io.http import HttpClient, NetworkError
from ..thumbnails import convert
from ..types import LookupContext, Metadata
from ..normalization import is_description_missing, is_placeholder_title
from ...io import paths


logger = logging.getLogger(__name__)


class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:

        if tag in ("script", "style"):
            self.hidden += 1

        elif tag in ("br", "p", "div", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:

        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)

        elif tag in ("p", "div", "li"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:

        if not self.hidden:
            self.parts.append(data)


class MetadataSource(ABC):
    """Implement fetch(); inherited lookup() isolates failures and validates individual fields."""

    name = ""
    provides: frozenset[str] = frozenset()
    hosts: frozenset[str] = frozenset()
    credential_hosts: frozenset[str] = frozenset()
    key_env: str | None = None
    identifies = False
    batch_size = 1

    def __init__(self, http: HttpClient, datasets: DatasetCache, *, key: str | None = None) -> None:
        self.http = http
        self.datasets = datasets
        self.key = key
        self.datasets.diagnostics = http.diagnostics

    def lookup(self, context: LookupContext) -> Metadata:
        return self.lookup_many([context])[0]

    def lookup_many(self, contexts: Sequence[LookupContext]) -> list[Metadata]:
        """Return one validated result per input, preserving order and isolating failed batches."""
        results = [Metadata() for _context in contexts]

        for index, result in self.iter_lookup(contexts):
            results[index] = result

        return results

    def get_skip_reason(self, context: LookupContext) -> str | None:

        if not self.identifies and not (context.missing & self.provides):
            return "already_filled"

        if self.key_env and not self.key:
            return "missing_key"

        return None

    def iter_batch_indexes(
        self, contexts: Sequence[LookupContext], pending: Sequence[int],
    ) -> Iterator[tuple[int, ...]]:
        """Group only eligible inputs; sources may group repeated verified identities together."""
        yield from batched(pending, self.batch_size)

    def iter_lookup(
        self, contexts: Sequence[LookupContext], *, executor: Executor | None = None, max_pending: int = 1,
    ) -> Iterator[tuple[int, Metadata]]:
        """Yield indexed results with bounded submitted work; the caller owns the executor."""

        if type(self.batch_size) is not int or not 1 <= self.batch_size <= 128:
            raise ValueError("Source batch size must be 1..128")

        if type(max_pending) is not int or not 1 <= max_pending <= 32:
            raise ValueError("Pending metadata batches must be 1..32")

        pending = []

        for index, context in enumerate(contexts):
            reason = self.get_skip_reason(context)

            if reason:
                yield index, self._finish(context, Metadata(outcome=reason), [])

            else:
                pending.append(index)

        batches = self.iter_batch_indexes(contexts, pending)

        if executor is None:

            for indexes in batches:
                yield from zip(indexes, self._lookup_batch([contexts[index] for index in indexes]), strict=True)

            return

        running: dict[Future, tuple[int, ...]] = {}

        try:
            exhausted = False

            while running or not exhausted:
                # Observe failures before replenishing the queue, including a failure
                # that completed while the caller was consuming a previous result.

                for future in running:

                    if future.done():
                        future.result()

                while not exhausted and len(running) < max_pending:
                    indexes = next(batches, None)

                    if indexes is None:
                        exhausted = True

                    else:
                        future = executor.submit(self._lookup_batch, [contexts[index] for index in indexes])
                        running[future] = indexes

                if not running:
                    break

                done, _pending_futures = wait(running, return_when=FIRST_COMPLETED)
                completed = [(future, future.result()) for future in done]

                for future, results in completed:
                    yield from zip(running.pop(future), results, strict=True)

        finally:

            for future in running:
                future.cancel()

    def _lookup_batch(self, group: Sequence[LookupContext]) -> list[Metadata]:
        rom = group[0].rom.entry["hash"] if len(group) == 1 else None

        with self.http.diagnostics.capture_events(self.name, rom) as events:

            if self.batch_size > 1:
                self.http.diagnostics.emit("batch", "lookup", size=len(group))

            try:
                fetched = self.fetch_many(group)

            except (NetworkError, OSError, ValueError) as exc:
                self.report_error(exc)
                fetched = [Metadata() for _context in group]

        # A broken source contract is a programming error, not a remote outage.

        if not isinstance(fetched, list) or len(fetched) != len(group):
            raise TypeError("Source batch must return one Metadata per context")

        if any(not isinstance(result, Metadata) for result in fetched):
            raise TypeError("Source batch returned an invalid Metadata result")

        return [self._finish(context, result, events) for context, result in zip(group, fetched, strict=True)]

    def _finish(self, context: LookupContext, result: Metadata, shared_events: list[dict]) -> Metadata:

        with self.http.diagnostics.capture_events(self.name, context.rom.entry["hash"]) as local_events:
            result = self._accept(context, result)
            events = shared_events + local_events
            issues = any(event["kind"] == "issue" for event in events)
            outcome = result.outcome

            if issues:
                outcome = "partial" if result.fields or result.thumbnail else "unavailable"

            elif outcome == "no_match":

                if any(event["outcome"] == "offline_miss" for event in events):
                    outcome = "offline_miss"

                elif any(event.get("status") == 404 or event["outcome"] == "cached_not_found" for event in events):
                    outcome = "not_found"

            self.http.diagnostics.emit(
                "lookup", outcome, fields=sorted(result.field_sources),
                matched=bool(result.sources or result.identifiers),
            )
            result.outcome = outcome
            return result

    def _accept(self, context: LookupContext, result: Metadata) -> Metadata:

        if is_placeholder_title(result.fields.get("title")):
            logger.warning(
                "%s — ZZZ metadata from %s ignored; ROM remains eligible for import",
                paths.quote_path(Path(context.rom.entry["image"]).name), self.name,
            )
            self.http.diagnostics.emit("metadata", "ignored_placeholder", title="ZZZ")
            # Drop the entire card, including IDs and images; its identity may be
            # valid while all descriptive fields belong to a generic category.
            return Metadata(outcome="ignored_placeholder")

        thumbnail = result.thumbnail if "thumbnail" in self.provides & context.missing else None
        accepted = Metadata(thumbnail=thumbnail, sources=result.sources, identifiers=result.identifiers)
        accepted.outcome = (
            "matched" if result.sources or result.identifiers or result.fields or thumbnail else result.outcome
        )

        for name, value in result.fields.items():

            if name not in self.provides or name not in context.missing or value is None:
                continue

            if name == "description" and is_description_missing(
                {
                    **context.rom.entry,
                    "title": self.normalize_text(result.fields.get("title")) or context.rom.entry["title"],
                    "description": value,
                }
            ):
                continue

            try:

                if name == "genre":
                    value = resolve_genre(value, context.rom.entry["image"], self.http.diagnostics)

                    if value is None:
                        continue

                validate([{**context.rom.entry, name: value}])

            except CatalogueError:
                logger.warning(
                    "%s — invalid %s from %s; ignored",
                    paths.quote_path(Path(context.rom.entry["image"]).name), name, self.name,
                )
                self.http.diagnostics.emit("issue", "invalid_field", field=name)

            else:
                accepted.fields[name] = value
                accepted.field_sources[name] = self.name

        if accepted.thumbnail is not None:
            accepted.field_sources["thumbnail"] = self.name

        return accepted

    def report_error(self, exc: Exception) -> None:
        # Reasons/statuses are local structured values; arbitrary exception messages may contain keys.
        reason = exc.reason if isinstance(exc, NetworkError) else type(exc).__name__
        status = exc.status if isinstance(exc, NetworkError) else None
        self.http.diagnostics.emit("issue", "source_error", reason=reason, status=status)
        logger.warning("Source %s unavailable: %s (HTTP status=%s)", self.name, reason, status)

    @abstractmethod
    def fetch(self, context: LookupContext) -> Metadata:
        """Return source-specific values and identifiers after checking game identity."""

    def fetch_many(self, contexts: Sequence[LookupContext]) -> list[Metadata]:
        """Override with native batching and batch_size; single-item sources need no changes."""
        return [self.fetch(context) for context in contexts]

    def get(self, url: str, *, image: bool = False) -> bytes | None:
        try:
            data = self.http.get(url, hosts=self.hosts, limit=8 * 1024 * 1024 if image else 4 * 1024 * 1024)
            return convert(data) if data is not None and image else data

        except (NetworkError, OSError, ValueError) as exc:

            if not image:
                raise

            self.report_error(exc)
            return None

    def get_json(self, url: str, *, headers: dict[str, str] | None = None, optional: bool = False) -> dict | None:

        if headers and urlsplit(url).hostname not in self.credential_hosts:
            raise NetworkError("Credentials are restricted to their source")

        try:
            return self.http.get_json(url, hosts=self.hosts, headers=headers, redact=(self.key,) if self.key else ())

        except NetworkError as exc:

            if not optional:
                raise

            self.report_error(exc)
            return None

    @staticmethod
    def require_mapping(value: object) -> dict:

        if not isinstance(value, dict):
            raise ValueError("Expected a metadata object")

        return value

    @staticmethod
    def normalize_text(value: object, *, html: bool = False, multiline: bool = False) -> str | None:

        if not isinstance(value, str):
            return None

        if html:
            parser = _PlainText()
            parser.feed(value)
            value = "".join(parser.parts)

        value = value.replace("\r\n", "\n").strip()

        if not value or not is_clean_text(value, multiline=multiline):
            return None

        return value.encode("utf-8")[:16384].decode("utf-8", errors="ignore")

    @staticmethod
    def parse_year(value: object) -> int | None:
        matches = re.findall(r"\b([12][0-9]{3})\b", f"{value}")
        return int(matches[0]) if len(set(matches)) == 1 else None

    @staticmethod
    def select_consensus_value(values: list) -> object:
        # Missing rows do not vote; conflicting nonempty values must not be guessed.
        known = [value for value in values if value is not None and value != ""]
        return known[0] if known and all(value == known[0] for value in known) else None


class FileMetadataSource(MetadataSource):
    """Persistent dataset acquisition shared by file-backed source subclasses."""

    dataset_namespace: str | None = None

    def obtain_dataset(
        self,
        name: str,
        url: str,
        *,
        download_limit: int,
        limit: int,
        validate: Callable[[Path], None] | None = None,
    ) -> Path | None:
        def download() -> bytes | None:
            return self.http.get(url, hosts=self.hosts, limit=download_limit, redirects=True, cache=False)

        return self.datasets.obtain(
            f"{self.dataset_namespace or self.name}-{name}",
            url,
            download=download,
            prepare=self.unpack,
            validate=validate or self.validate_dataset,
            limit=limit,
        )

    def unpack(self, data: bytes) -> bytes:
        return data

    @abstractmethod
    def validate_dataset(self, path: Path) -> None:
        """Raise ValueError for an unusable dataset before replacing its manifest."""
