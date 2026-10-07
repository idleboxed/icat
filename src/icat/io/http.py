"""Bounded GET transport with per-host pacing, cancellable quota waits and diagnostics."""

import hashlib
import json
import logging
import math
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from time import monotonic, time
from urllib.parse import quote, quote_plus, urljoin, urlsplit

import requests

from .. import __version__
from ..reporting.diagnostics import Diagnostics, NetworkActivity
from .files import write_atomic, ensure_directory, read_bytes
from . import paths


logger = logging.getLogger(__name__)
DEFAULT_REQUEST_INTERVALS = {
    "api.github.com": 1.0,
    "api.thegamesdb.net": 1.0,
    "hasheous.org": 1.0,
}
DEFAULT_RETRIES = 2
NOT_FOUND_TTL = 24 * 60 * 60


def redact_json(data: bytes, secrets: Sequence[str]) -> bytes:
    """Remove credential echoes before a response can reach the persistent HTTP cache."""
    values = sorted(
        {encoded for value in secrets if value for encoded in (value, quote(value, safe=""), quote_plus(value))},
        key=len, reverse=True,
    )

    def scrub(value: object) -> object:

        if isinstance(value, str):

            for secret in values:
                value = value.replace(secret, "[redacted]")

            return value

        if isinstance(value, list):
            return [scrub(item) for item in value]

        if isinstance(value, dict):
            return {scrub(key): scrub(item) for key, item in value.items()}

        return value

    try:
        value = json.loads(data)

        if not isinstance(value, dict):
            raise ValueError

        return json.dumps(scrub(value), ensure_ascii=False, separators=(",", ":")).encode()

    except (ValueError, UnicodeError, RecursionError):
        raise NetworkError("Invalid provider JSON", reason="invalid_json") from None


class NetworkError(Exception):
    """Only locally constructed messages; never include response bodies or credential URLs."""

    def __init__(self, message: str, *, reason: str = "response", status: int | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.status = status


def parse_retry_after(value: str | None) -> float:
    """Accept delta seconds and HTTP dates; missing/malformed values use a conservative minute."""
    try:

        if value and value.strip().isdigit():
            return max(1, min(float(value), 1e9))

        date = parsedate_to_datetime(value or "")

        if date.tzinfo is None:
            date = date.replace(tzinfo=UTC)

        return max(1, (date - datetime.now(UTC)).total_seconds())

    except (ValueError, TypeError, OverflowError):
        return 60


class HttpClient:
    def __init__(
        self,
        cache: Path,
        *,
        concurrency: int = 5,
        timeout: float = 20,
        offline: bool = False,
        refresh: bool = False,
        request_intervals: dict[str, float] | None = None,
        max_retries: int | None = None,
        max_wait: float = 180,
    ) -> None:
        request_intervals = DEFAULT_REQUEST_INTERVALS if request_intervals is None else request_intervals
        max_retries = DEFAULT_RETRIES if max_retries is None else max_retries

        if not 1 <= concurrency <= 32 or not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise ValueError("Concurrency must be 1..32 and timeout must be >0 and <=300")

        if any(
            not isinstance(host, str) or not host
            or not math.isfinite(interval) or not 0 <= interval <= 60
            for host, interval in request_intervals.items()
        ):
            raise ValueError("Host request intervals must be finite and 0..60 seconds")

        if not 0 <= max_retries <= 5 or not math.isfinite(max_wait) or not 0 <= max_wait <= 3600:
            raise ValueError("Retries must be 0..5 and maximum wait must be 0..3600 seconds")

        self.cache, self.timeout = cache, timeout
        self.offline, self.refresh = offline, refresh
        self.request_intervals = dict(request_intervals)
        self.max_retries, self.max_wait = max_retries, max_wait
        self.slots = threading.BoundedSemaphore(concurrency)
        self.cooldowns: dict[str, float] = {}
        self.next_request: dict[str, float] = {}
        self.auth_failures: dict[str, int] = {}
        self.not_found: dict[str, float] = {}
        self.guard = threading.Lock()
        self.cancelled = threading.Event()
        self.diagnostics = Diagnostics()

    def cancel(self) -> None:
        self.cancelled.set()

    def get_settings(self) -> dict:
        return {
            "offline": self.offline, "refresh": self.refresh, "timeout_seconds": self.timeout,
            "request_intervals_seconds": dict(sorted(self.request_intervals.items())),
            "max_retries": self.max_retries,
            "max_wait_seconds": self.max_wait,
        }

    @contextmanager
    def acquire_request_slot(self, host: str, scope: str, deadline: float) -> Iterator[None]:
        announced = set()

        while True:

            if self.cancelled.is_set():
                raise NetworkError("HTTP work cancelled", reason="cancelled")

            with self.guard:
                status = self.auth_failures.get(scope)
                quota = self.cooldowns.get(host, 0)
                ready = max(quota, self.next_request.get(host, 0))

            if status:
                self.diagnostics.emit("http", "auth_blocked", host=host, status=status, sent=False)
                raise NetworkError(
                    f"Authentication cooldown active: HTTP {status} from {host}", reason="auth", status=status
                )

            now = monotonic()
            delay = ready - now

            if delay > 0:
                reason = "rate_limit" if quota > now else "pacing"

                if now + delay > deadline:
                    self.diagnostics.emit(
                        "http", "wait_budget_exhausted", host=host, sent=False, retry_after_seconds=round(delay, 3)
                    )
                    raise NetworkError(f"Rate-limit/pacing wait budget exhausted for {host}", reason="wait_budget")

                if reason not in announced:
                    self.diagnostics.emit("wait", reason, host=host, seconds=round(delay, 3))
                    announced.add(reason)

                with self.diagnostics.track_activity(NetworkActivity(host, reason, ready)):
                    self.cancelled.wait(delay)

                continue

            # Never occupy a global HTTP slot while sleeping for another host's quota.

            with self.diagnostics.track_activity(NetworkActivity(host, "slot")):
                acquired = self.slots.acquire(timeout=0.1)

            if not acquired:

                if monotonic() > deadline:
                    raise NetworkError(f"HTTP slot wait budget exhausted for {host}", reason="wait_budget")

                continue

            with self.guard:
                now = monotonic()

                if self.auth_failures.get(scope) or max(
                    self.cooldowns.get(host, 0), self.next_request.get(host, 0)
                ) > now:
                    self.slots.release()
                    continue

                self.next_request[host] = now + self.request_intervals.get(host, 0.0)

            break

        try:

            with self.diagnostics.track_activity(NetworkActivity(host, "request")):
                yield

        finally:
            self.slots.release()

    def get(
        self,
        url: str,
        *,
        hosts: frozenset[str],
        limit: int = 4 * 1024 * 1024,
        headers: dict[str, str] | None = None,
        redirects: bool = False,
        cache: bool = True,
        response_filter: Callable[[bytes], bytes] | None = None,
    ) -> bytes | None:
        host = self.check_url(url, hosts)
        identity = url + (json.dumps(headers, sort_keys=True) if headers else "")
        key = hashlib.sha256(identity.encode()).hexdigest()
        path = self.cache / paths.HTTP_CACHE_DIR / key[:2] / key
        negative = path.with_suffix(".404")

        with self.guard:
            checked = self.not_found.get(key)

        missing = checked is not None and (self.offline or 0 <= time() - checked < NOT_FOUND_TTL)

        if cache and not missing and negative.exists() and (self.offline or not self.refresh):
            age = time() - negative.stat().st_mtime

            if self.offline or 0 <= age < NOT_FOUND_TTL:
                try:
                    missing = read_bytes(negative, 16) == b"404\n"

                except ValueError:
                    missing = False

                if not missing:
                    self.diagnostics.emit("http", "invalid_negative_cache", host=host, request=key, sent=False)

        if missing and cache:
            self.diagnostics.emit("http", "cached_not_found", host=host, request=key, sent=False)
            return None

        if cache and path.exists() and (self.offline or not self.refresh):

            if self.offline or time() - path.stat().st_mtime < 30 * 86400:
                data = read_bytes(path, limit)

                if response_filter is not None:
                    filtered = response_filter(data)

                    if len(filtered) > limit:
                        raise NetworkError("Filtered response exceeds size limit", reason="response_limit")

                    if filtered != data:
                        # Keep cached responses subject to the same current redaction policy.
                        write_atomic(path, filtered)

                    data = filtered

                self.diagnostics.emit("http", "cache_hit", host=host, request=key, sent=False)
                return data

        if self.offline:
            self.diagnostics.emit("http", "offline_miss", host=host, request=key, sent=False)
            return None

        request_headers = {
            "User-Agent": f"icat/{__version__} (IGUI catalogue builder)", "Accept": "application/json,image/*,*/*",
        }
        request_headers.update(headers or {})
        deadline = monotonic() + self.max_wait
        retries = hops = 0

        while True:
            host = self.check_url(url, hosts)
            scope = host + (
                hashlib.sha256(json.dumps(headers, sort_keys=True).encode()).hexdigest() if headers else ""
            )
            failure = None
            wait = 0
            status = None

            with self.acquire_request_slot(host, scope, deadline):
                started = monotonic()
                try:

                    with requests.get(
                        url, headers=request_headers, timeout=(self.timeout, self.timeout),
                        stream=True, allow_redirects=False,
                    ) as response:
                        status = response.status_code
                        retry_header = response.headers.get("Retry-After")
                        wait = parse_retry_after(retry_header) if status in (429, 503) or (
                            status in (500, 502, 504) and retry_header is not None
                        ) else 0

                        if status in (401, 403):

                            with self.guard:
                                self.auth_failures[scope] = status

                        elif wait:

                            with self.guard:
                                self.cooldowns[host] = max(self.cooldowns.get(host, 0), monotonic() + wait)

                            logger.warning("HTTP %d from %s; retry delay %.1f seconds", status, host, wait)

                        if status == 200:
                            chunks, size = [], 0

                            for chunk in response.iter_content(64 * 1024):

                                if self.cancelled.is_set():
                                    raise NetworkError("HTTP work cancelled", reason="cancelled")

                                size += len(chunk)

                                if size > limit or monotonic() - started > self.timeout * 3:
                                    raise NetworkError("Response exceeds size/time limit", reason="response_limit")

                                chunks.append(chunk)

                            data = b"".join(chunks)

                        elif status in (301, 302, 303, 307, 308) and redirects and not headers and hops < 3:
                            target = response.headers.get("Location")

                            if not target:
                                raise NetworkError("Redirect has no destination", reason="redirect")

                            url = urljoin(url, target)
                            self.check_url(url, hosts)
                            hops += 1

                        elif status not in (200, 404):
                            failure = NetworkError(f"HTTP {status} from {host}", reason="http_status", status=status)

                except requests.RequestException as exc:
                    failure = NetworkError(
                        f"Request failed for {host}: {type(exc).__name__}", reason=type(exc).__name__
                    )

                except NetworkError as exc:
                    failure = exc

                except BaseException as exc:
                    failure = NetworkError("Request interrupted", reason=type(exc).__name__)
                    raise

                finally:
                    self.diagnostics.emit(
                        "http", "response" if failure is None or failure.status else "failure",
                        host=host, request=key, sent=True, status=status, attempt=retries + 1,
                        duration_seconds=round(monotonic() - started, 6),
                        reason=failure.reason if failure else None, retry_after_seconds=wait or None,
                    )

            if failure is None:

                if status == 404:

                    with self.guard:
                        self.not_found[key] = time()

                    if cache:
                        ensure_directory(path.parent)
                        write_atomic(negative, b"404\n")
                        # A refreshed 404 must not resurrect an older successful body later.
                        path.unlink(missing_ok=True)

                    return None

                if status == 200:

                    if response_filter is not None:
                        data = response_filter(data)

                        if len(data) > limit:
                            raise NetworkError("Filtered response exceeds size limit", reason="response_limit")

                    if cache:
                        ensure_directory(path.parent)
                        write_atomic(path, data)
                        negative.unlink(missing_ok=True)

                        with self.guard:
                            self.not_found.pop(key, None)

                    return data

                continue

            transient = failure.status in (429, 500, 502, 503, 504) or failure.reason in (
                "Timeout", "ConnectTimeout", "ReadTimeout", "ConnectionError"
            )

            if not transient or retries >= self.max_retries:
                raise failure

            retries += 1

            if not wait:
                wait = min(2 ** retries, 30)

                with self.guard:
                    self.cooldowns[host] = max(self.cooldowns.get(host, 0), monotonic() + wait)

            self.diagnostics.emit(
                "http", "retry_scheduled", host=host, sent=False, status=status,
                reason=failure.reason, attempt=retries + 1, retry_after_seconds=wait,
            )

    @staticmethod
    def check_url(url: str, hosts: frozenset[str]) -> str:
        try:
            parsed = urlsplit(url)
            valid = parsed.scheme == "https" and parsed.hostname in hosts and not parsed.username and not parsed.port

        except ValueError:
            valid = False

        if not valid:
            raise NetworkError("Untrusted metadata URL", reason="untrusted_url")

        return parsed.hostname

    def get_json(
        self, url: str, *, hosts: frozenset[str], headers: dict[str, str] | None = None,
        limit: int = 4 * 1024 * 1024,
        redact: Sequence[str] = (),
    ) -> dict | None:
        def filter_response(data: bytes) -> bytes:
            return redact_json(data, redact)

        response_filter = filter_response if redact else None
        data = self.get(url, hosts=hosts, headers=headers, limit=limit, response_filter=response_filter)

        if data is None:
            return None

        try:
            value = json.loads(data)

        except (ValueError, UnicodeError):
            raise NetworkError("Invalid provider JSON", reason="invalid_json") from None

        if not isinstance(value, dict):
            raise NetworkError("Expected a metadata object", reason="invalid_json")

        return value
