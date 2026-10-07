import json
import os
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
import requests
from time_machine import TimeMachineFixture

from icat.reporting.diagnostics import Diagnostics
from icat.io.http import HttpClient, NetworkError
from icat.io import http as network


HOSTS = frozenset({"hasheous.org", "raw.githubusercontent.com", "images.example.test"})
URL = "https://hasheous.org/api/v1/test"


@pytest.mark.parametrize("failure", [OSError("Synthetic failure"), KeyboardInterrupt()])
def test_request_activity_and_slot_are_released_on_error(tmp_path: Path, failure: BaseException) -> None:
    client = HttpClient(tmp_path)

    with pytest.raises(type(failure), match="Synthetic failure" if isinstance(failure, OSError) else "^$"):

        with client.acquire_request_slot("hasheous.org", "scope", float("inf")):
            activities = client.diagnostics.get_activity_snapshot()
            assert len(activities) == 1
            assert activities[0].state == "request"
            raise failure

    assert client.diagnostics.get_activity_snapshot() == ()

    with client.acquire_request_slot("hasheous.org", "scope", float("inf")):
        assert len(client.diagnostics.get_activity_snapshot()) == 1


def test_rate_limit_activity_clears_after_cancel_without_saving_live_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = HttpClient(tmp_path)
    client.cooldowns["hasheous.org"] = 110

    def get_monotonic_time() -> float:
        return 100

    monkeypatch.setattr("icat.io.http.monotonic", get_monotonic_time)

    def wait(seconds: float) -> None:
        activity, = client.diagnostics.get_activity_snapshot()
        assert activity.host == "hasheous.org"
        assert activity.state == "rate_limit"
        assert activity.until == 110
        assert seconds == 10
        client.cancel()

    monkeypatch.setattr(client.cancelled, "wait", wait)

    with pytest.raises(NetworkError, match="cancelled"):

        with client.acquire_request_slot("hasheous.org", "scope", 120):
            pytest.fail("Cancelled request must not acquire a slot")

    assert client.diagnostics.get_activity_snapshot() == ()
    snapshot = client.diagnostics.get_snapshot()
    assert snapshot["counts"]["transport"]["wait.rate_limit"] == 1
    assert "activities" not in snapshot


def test_requests_cache_and_offline_do_not_repeat_network(tmp_path: Path, response_mock: Callable[..., Any]) -> None:
    client = HttpClient(tmp_path)

    with response_mock(f"GET {URL} -> 200 :{{\"ok\":true}}") as mock:
        assert client.get_json(URL, hosts=HOSTS) == {"ok": True}
        assert client.get_json(URL, hosts=HOSTS) == {"ok": True}
        assert HttpClient(tmp_path, offline=True).get_json(URL, hosts=HOSTS) == {"ok": True}
        assert HttpClient(tmp_path, offline=True).get(f"{URL}/missing", hosts=HOSTS) is None
        assert len(mock.calls) == 1
        assert mock.calls[0].request.req_kwargs["timeout"] == (20, 20)
        assert mock.calls[0].response.raw.closed


def test_credential_echo_is_redacted_before_any_cache_write(
    tmp_path: Path, response_mock: Callable[..., Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "synthetic-secret"
    private = f"{URL}?apikey={secret}"
    original_write = network.write_atomic
    writes = []

    def check_and_write(path: Path, data: bytes) -> None:
        assert secret.encode() not in data
        writes.append(path)
        return original_write(path, data)

    monkeypatch.setattr(network, "write_atomic", check_and_write)
    body = json.dumps({"pages": {"current": private}, "nested": [{secret: secret}], "players": 2})

    with response_mock(f"GET {private} -> 200 :{body}"):
        value = HttpClient(tmp_path).get_json(private, hosts=HOSTS, redact=(secret,))

    assert secret not in json.dumps(value)
    assert value["players"] == 2
    assert len(writes) == 1
    assert all(secret.encode() not in path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())


def test_unfiltered_cached_credential_echo_is_scrubbed_offline(
    tmp_path: Path, response_mock: Callable[..., Any],
) -> None:
    secret = "synthetic-secret"
    private = f"{URL}?apikey={secret}"

    with response_mock(f"GET {private} -> 200 :{json.dumps({"echo": secret})}"):
        HttpClient(tmp_path).get(private, hosts=HOSTS)

    with response_mock([]) as mock:
        value = HttpClient(tmp_path, offline=True).get_json(private, hosts=HOSTS, redact=(secret,))
        assert not mock.calls

    assert value == {"echo": "[redacted]"}
    assert all(secret.encode() not in path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())


@pytest.mark.parametrize("body", ["synthetic-secret invalid-json", "[\"synthetic-secret\"]"])
def test_invalid_sensitive_json_is_not_cached(tmp_path: Path, response_mock: Callable[..., Any], body: str) -> None:

    with response_mock(f"GET {URL} -> 200 :{body}"):

        with pytest.raises(NetworkError, match="JSON"):
            HttpClient(tmp_path).get_json(URL, hosts=HOSTS, redact=("synthetic-secret",))

    assert not list(tmp_path.rglob("*"))


def test_sensitive_json_redacts_escaped_and_urlencoded_values(
    tmp_path: Path, response_mock: Callable[..., Any],
) -> None:
    secret = "synthetic \"key\" +/ю"
    body = json.dumps({"plain": secret, "url": quote(secret, safe="")})

    with response_mock(f"GET {URL} -> 200 :{body}"):
        value = HttpClient(tmp_path).get_json(URL, hosts=HOSTS, redact=(secret,))

    assert value == {"plain": "[redacted]", "url": "[redacted]"}


def test_response_filter_cannot_exceed_cache_size_limit(tmp_path: Path, response_mock: Callable[..., Any]) -> None:
    def make_oversized_response(data: bytes) -> bytes:
        return b"oversized"

    with response_mock(f"GET {URL} -> 200 :ok"):

        with pytest.raises(NetworkError, match="Filtered response exceeds"):
            HttpClient(tmp_path).get(URL, hosts=HOSTS, limit=2, response_filter=make_oversized_response)

    assert not list(tmp_path.rglob("*"))


@pytest.mark.parametrize("status", [401, 403])
def test_auth_and_rate_limits_activate_cooldown_without_retry(
    tmp_path: Path, response_mock: Callable[..., Any], status: int,
) -> None:
    client = HttpClient(tmp_path)

    with response_mock(f"GET {URL}\nRetry-After: 120\n-> {status} :") as mock:

        with pytest.raises(NetworkError, match=f"HTTP {status}"):
            client.get(URL, hosts=HOSTS)

        with pytest.raises(NetworkError, match="cooldown"):
            client.get(URL, hosts=HOSTS)

        assert len(mock.calls) == 1


@pytest.mark.parametrize("data", [b"invalid-json", b"[]"])
def test_bad_json_is_an_expected_error(tmp_path: Path, response_mock: Callable[..., Any], data: bytes) -> None:

    with response_mock(f"GET {URL} -> 200 :".encode() + data):

        with pytest.raises(NetworkError, match="JSON|object"):
            HttpClient(tmp_path).get_json(URL, hosts=HOSTS)


def test_response_limit_and_failure_release_resources(tmp_path: Path, response_mock: Callable[..., Any]) -> None:
    client = HttpClient(tmp_path, concurrency=1)

    with response_mock([f"GET {URL} -> 200 :oversized", f"GET {URL} -> 200 :{{\"ok\":true}}"]) as mock:

        with pytest.raises(NetworkError, match="size/time"):
            client.get(URL, hosts=HOSTS, limit=2)

        assert mock.calls[0].response.raw.closed
        assert client.get_json(URL, hosts=HOSTS) == {"ok": True}
        assert len(mock.calls) == 2


def test_transport_does_not_log_secrets_from_requests_exception(
    tmp_path: Path, response_mock: Callable[..., Any],
) -> None:

    with response_mock([]) as mock:
        mock.add("GET", URL, body=requests.Timeout("a private token"))

        with pytest.raises(NetworkError, match="Timeout") as exc:
            HttpClient(tmp_path).get(URL, hosts=HOSTS)

        assert "private token" not in f"{exc.value}"
        assert len(mock.calls) == 1


def test_global_concurrency_limit_applies_to_all_hosts(
    tmp_path: Path, response_mock: Callable[..., Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = threading.Lock()
    readers_ready = threading.Event()
    release_readers = threading.Event()
    active = maximum = 0
    original_iter_content = requests.Response.iter_content

    def wait_and_read(response: requests.Response, *args: Any, **kwargs: Any) -> Iterator[bytes | str]:
        nonlocal active, maximum

        with guard:
            active += 1
            maximum = max(active, maximum)

            if active == 2:
                readers_ready.set()

        try:
            assert release_readers.wait(5), "Test did not release HTTP readers"
            yield from original_iter_content(response, *args, **kwargs)

        finally:

            with guard:
                active -= 1

    # Keep two response bodies open; responses themselves come from response_mock.
    monkeypatch.setattr(requests.Response, "iter_content", wait_and_read)
    client = HttpClient(tmp_path, concurrency=2)
    hosts = ("hasheous.org", "raw.githubusercontent.com", "images.example.test")
    urls = [f"https://{hosts[number % len(hosts)]}/test/{number}" for number in range(16)]

    with response_mock([f"GET {url} -> 200 :ok" for url in urls]) as mock:

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(client.get, url, hosts=HOSTS) for url in urls]
            try:
                assert readers_ready.wait(5), "HTTP bodies were not read concurrently"

            finally:
                release_readers.set()

            assert [future.result() for future in futures] == [b"ok"] * len(urls)

        assert len(mock.calls) == len(urls)

    assert maximum == 2


def test_credentials_are_explicit_per_request_and_never_implicitly_forwarded(
    tmp_path: Path, response_mock: Callable[..., Any],
) -> None:
    image_url = "https://raw.githubusercontent.com/test/image.png"
    client = HttpClient(tmp_path)

    with response_mock([f"GET {URL} -> 200 :ok", f"GET {image_url} -> 200 :image"]) as mock:
        client.get(URL, hosts=HOSTS, headers={"X-Client-API-Key": "synthetic-key"})
        client.get(image_url, hosts=HOSTS)

        assert mock.calls[0].request.headers["X-Client-API-Key"] == "synthetic-key"
        assert "X-Client-API-Key" not in mock.calls[1].request.headers

        with pytest.raises(NetworkError, match="Untrusted"):
            client.get("http://localhost/secret", hosts=HOSTS)

        assert len(mock.calls) == 2


def test_redirect_and_not_found_never_follow_unknown_urls(tmp_path: Path, response_mock: Callable[..., Any]) -> None:
    client = HttpClient(tmp_path)
    rules = [f"GET {URL}\nLocation: https://example.invalid/redirect\n-> 302 :", f"GET {URL} -> 404 :"]

    with response_mock(rules) as mock:

        with pytest.raises(NetworkError, match="HTTP 302"):
            client.get(URL, hosts=HOSTS)

        assert client.get(URL, hosts=HOSTS) is None
        assert [call.request.url for call in mock.calls] == [URL, URL]


def test_dataset_redirects_are_bounded_and_checked_before_each_request(
    tmp_path: Path, response_mock: Callable[..., Any],
) -> None:
    first = "https://github.com/test/database.zip"
    second = "https://release-assets.githubusercontent.com/database.zip"
    hosts = frozenset({"github.com", "release-assets.githubusercontent.com"})
    rules = [f"GET {first}\nLocation: {second}\n-> 302 :", f"GET {second} -> 200 :database"]

    with response_mock(rules) as mock:
        assert HttpClient(tmp_path).get(first, hosts=hosts, redirects=True, cache=False) == b"database"
        assert len(mock.calls) == 2

    assert not list(tmp_path.iterdir())

    with response_mock(f"GET {first}\nLocation: http://localhost/private\n-> 302 :") as mock:

        with pytest.raises(NetworkError, match="Untrusted"):
            HttpClient(tmp_path).get(first, hosts=hosts, redirects=True, cache=False)

        assert len(mock.calls) == 1


def test_credentialed_redirect_is_not_followed_even_when_requested(
    tmp_path: Path, response_mock: Callable[..., Any],
) -> None:

    with response_mock(f"GET {URL}\nLocation: https://images.example.test/private\n-> 302 :") as mock:

        with pytest.raises(NetworkError, match="HTTP 302"):
            HttpClient(tmp_path).get(URL, hosts=HOSTS, redirects=True, headers={"X-Key": "private"})

        assert len(mock.calls) == 1


def test_authenticated_cache_is_separated_between_credentials(
    tmp_path: Path, response_mock: Callable[..., Any],
) -> None:
    client = HttpClient(tmp_path)

    with response_mock([f"GET {URL} -> 200 :first", f"GET {URL} -> 200 :second"]) as mock:
        assert client.get(URL, hosts=HOSTS, headers={"X-Key": "one"}) == b"first"
        assert client.get(URL, hosts=HOSTS, headers={"X-Key": "two"}) == b"second"
        assert client.get(URL, hosts=HOSTS, headers={"X-Key": "one"}) == b"first"
        assert len(mock.calls) == 2


def test_bad_proxy_key_does_not_disable_anonymous_hash_lookup_on_same_host(
    tmp_path: Path, response_mock: Callable[..., Any],
) -> None:
    client = HttpClient(tmp_path)
    lookup = f"{URL}/lookup"

    with response_mock([f"GET {URL} -> 401 :", f"GET {lookup} -> 200 :ok"]) as mock:

        with pytest.raises(NetworkError, match="HTTP 401"):
            client.get(URL, hosts=HOSTS, headers={"X-Client-API-Key": "bad-key"})

        assert client.get(lookup, hosts=HOSTS) == b"ok"
        assert len(mock.calls) == 2


@pytest.fixture
def virtual_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [0.0]

    def get_monotonic_time() -> float:
        return now[0]

    monkeypatch.setattr("icat.io.http.monotonic", get_monotonic_time)
    return now


def test_429_waits_and_retries_the_same_rom_without_holding_http_slot(
    tmp_path: Path, response_mock: Callable[..., Any], monkeypatch: pytest.MonkeyPatch, virtual_clock: list[float],
) -> None:
    client = HttpClient(tmp_path, concurrency=1, max_retries=2)
    waits = []

    def wait(seconds: float) -> None:
        assert client.slots.acquire(blocking=False)
        client.slots.release()
        waits.append(seconds)
        virtual_clock[0] += seconds

    monkeypatch.setattr(client.cancelled, "wait", wait)
    rules = [f"GET {URL}\nRetry-After: 17\n-> 429 :", f"GET {URL} -> 200 :ok"]

    with response_mock(rules) as mock:
        assert client.get(URL, hosts=HOSTS) == b"ok"
        assert len(mock.calls) == 2

    assert waits == [17]
    stats = client.diagnostics.get_snapshot()["hosts"]["hasheous.org"]
    assert stats["HTTP_429"] == stats["HTTP_200"] == 1
    assert stats["requests_sent"] == 2


def test_retry_after_http_date_is_not_treated_as_default_minute(
    tmp_path: Path, response_mock: Callable[..., Any], monkeypatch: pytest.MonkeyPatch,
    virtual_clock: list[float], time_machine: TimeMachineFixture,
) -> None:
    time_machine.move_to(datetime(2026, 10, 6, 0, 0, tzinfo=UTC), tick=False)
    client = HttpClient(tmp_path, max_retries=1)
    waits = []
    date = format_datetime(datetime.now(UTC) + timedelta(seconds=45), usegmt=True)

    def wait(seconds: float) -> None:
        waits.append(seconds)
        virtual_clock[0] += seconds

    monkeypatch.setattr(client.cancelled, "wait", wait)

    with response_mock([f"GET {URL}\nRetry-After: {date}\n-> 503 :", f"GET {URL} -> 200 :ok"]):
        assert client.get(URL, hosts=HOSTS) == b"ok"

    assert waits == [45.0]


@pytest.mark.parametrize("retry", ["600", "999999999999999999999999999999999999999"])
def test_retry_after_beyond_budget_never_causes_an_early_request(
    tmp_path: Path, response_mock: Callable[..., Any], virtual_clock: list[float], retry: str,
) -> None:
    client = HttpClient(tmp_path, max_retries=2, max_wait=30)

    with response_mock(f"GET {URL}\nRetry-After: {retry}\n-> 429 :") as mock:

        with pytest.raises(NetworkError, match="budget"):
            client.get(URL, hosts=HOSTS)

        with pytest.raises(NetworkError, match="budget"):
            client.get(f"{URL}/another-rom", hosts=HOSTS)

        assert len(mock.calls) == 1

    stats = client.diagnostics.get_snapshot()["hosts"]["hasheous.org"]
    assert stats["HTTP_429"] == 1
    assert stats["http.wait_budget_exhausted"] == 2


def test_pacing_is_shared_by_requests_to_a_host_but_not_other_hosts(
    tmp_path: Path, response_mock: Callable[..., Any], monkeypatch: pytest.MonkeyPatch, virtual_clock: list[float],
) -> None:
    client = HttpClient(tmp_path, request_intervals={"hasheous.org": 2})
    waits = []

    def wait(seconds: float) -> None:
        waits.append(seconds)
        virtual_clock[0] += seconds

    monkeypatch.setattr(client.cancelled, "wait", wait)
    urls = [URL, "https://images.example.test/other", f"{URL}/next"]

    with response_mock([f"GET {url} -> 200 :ok" for url in urls]):

        for url in urls:
            assert client.get(url, hosts=HOSTS) == b"ok"

    assert waits == [2]


def test_transient_retries_are_bounded_and_authentication_is_not_retried(
    tmp_path: Path, response_mock: Callable[..., Any], monkeypatch: pytest.MonkeyPatch, virtual_clock: list[float],
) -> None:
    client = HttpClient(tmp_path, max_retries=2)

    def wait(seconds: float) -> None:
        virtual_clock[0] += seconds

    monkeypatch.setattr(client.cancelled, "wait", wait)

    with response_mock([f"GET {URL} -> 502 :"] * 3) as mock:

        with pytest.raises(NetworkError, match="HTTP 502"):
            client.get(URL, hosts=HOSTS)

        assert len(mock.calls) == 3

    assert virtual_clock[0] == 6
    client = HttpClient(tmp_path, max_retries=2)

    with response_mock(f"GET {URL} -> 403 :") as mock:

        with pytest.raises(NetworkError, match="HTTP 403"):
            client.get(URL, hosts=HOSTS)

        assert len(mock.calls) == 1


def test_quota_wait_is_cancellable(
    tmp_path: Path, response_mock: Callable[..., Any], monkeypatch: pytest.MonkeyPatch, virtual_clock: list[float],
) -> None:
    client = HttpClient(tmp_path, max_retries=2)

    def wait(seconds: float) -> None:
        client.cancel()

    monkeypatch.setattr(client.cancelled, "wait", wait)

    with response_mock(f"GET {URL}\nRetry-After: 120\n-> 429 :") as mock:

        with pytest.raises(NetworkError, match="cancelled"):
            client.get(URL, hosts=HOSTS)

        assert len(mock.calls) == 1

    assert client.slots.acquire(blocking=False)


def test_diagnostics_distinguish_not_found_cache_and_private_transport_failure(
    tmp_path: Path, response_mock: Callable[..., Any],
) -> None:
    client = HttpClient(tmp_path)
    private = f"{URL}?apikey=synthetic-secret"

    with response_mock([f"GET {URL} -> 404 :"]) as mock:
        assert client.get(URL, hosts=HOSTS) is None
        assert client.get(URL, hosts=HOSTS) is None
        mock.add("GET", private, body=requests.ConnectionError("synthetic-secret"))

        with pytest.raises(NetworkError, match="ConnectionError"):
            client.get(private, hosts=HOSTS, headers={"X-Key": "another-secret"})

    diagnostics = client.diagnostics.get_snapshot()
    assert "synthetic-secret" not in json.dumps(diagnostics)
    assert "another-secret" not in json.dumps(diagnostics)
    assert diagnostics["hosts"]["hasheous.org"]["http.cached_not_found"] == 1
    assert diagnostics["hosts"]["hasheous.org"]["requests_sent"] == 2


def test_recovered_timeouts_keep_one_safe_failure_group_after_event_limit(
    tmp_path: Path, response_mock: Callable[..., Any], monkeypatch: pytest.MonkeyPatch, virtual_clock: list[float],
) -> None:
    client = HttpClient(tmp_path, max_retries=1)
    client.diagnostics = Diagnostics(event_limit=0)

    def wait(seconds: float) -> None:
        virtual_clock[0] += seconds

    monkeypatch.setattr(client.cancelled, "wait", wait)

    with response_mock([]) as mock:

        for number in range(3):
            url = f"{URL}/{number}?apikey=synthetic-secret"
            mock.add("GET", url, body=requests.ReadTimeout("private response detail"))
            mock.add("GET", url, body=b"ok")

            with client.diagnostics.capture_events("fixture", f"{number}"):
                assert client.get(url, hosts=HOSTS) == b"ok"

    diagnostics = client.diagnostics.get_snapshot()
    assert diagnostics["events"] == []
    assert len(diagnostics["network_failures"]) == 1
    group = diagnostics["network_failures"][0]
    assert group["count"] == 3 and group["reason"] == "ReadTimeout"
    assert group["first"]["rom"] == "0" and group["last"]["rom"] == "2"
    assert diagnostics["hosts"]["hasheous.org"]["requests_sent"] == 6
    assert "synthetic-secret" not in json.dumps(diagnostics)
    assert "private response detail" not in json.dumps(diagnostics)


def test_404_cache_survives_client_restart_without_retaining_private_url(
    tmp_path: Path, response_mock: Callable[..., Any],
) -> None:
    private = f"{URL}?apikey=synthetic-secret"

    with response_mock(f"GET {private} -> 404 :private response") as mock:
        assert HttpClient(tmp_path).get(private, hosts=HOSTS) is None
        second = HttpClient(tmp_path)
        assert second.get(private, hosts=HOSTS) is None
        assert len(mock.calls) == 1

    assert second.diagnostics.get_snapshot()["hosts"]["hasheous.org"]["http.cached_not_found"] == 1
    files = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert len(files) == 1
    assert files[0].read_bytes() == b"404\n"
    assert "synthetic-secret" not in f"{files[0]}"


def test_expired_404_is_refetched_but_offline_can_use_it(
    tmp_path: Path, response_mock: Callable[..., Any], monkeypatch: pytest.MonkeyPatch,
) -> None:

    with response_mock(f"GET {URL} -> 404 :"):
        assert HttpClient(tmp_path).get(URL, hosts=HOSTS) is None

    marker = next(tmp_path.rglob("*.404"))
    os.utime(marker, (100, 100))
    def get_current_time() -> float:
        return 100 + 86400

    monkeypatch.setattr("icat.io.http.time", get_current_time)

    with response_mock([]) as mock:
        assert HttpClient(tmp_path, offline=True, refresh=True).get(URL, hosts=HOSTS) is None
        assert not mock.calls

    with response_mock(f"GET {URL} -> 200 :found") as mock:
        assert HttpClient(tmp_path).get(URL, hosts=HOSTS) == b"found"
        assert len(mock.calls) == 1

    assert not marker.exists()


def test_refresh_bypasses_negative_cache_and_invalidates_old_success(
    tmp_path: Path, response_mock: Callable[..., Any],
) -> None:

    with response_mock(f"GET {URL} -> 200 :old"):
        assert HttpClient(tmp_path).get(URL, hosts=HOSTS) == b"old"

    with response_mock(f"GET {URL} -> 404 :"):
        assert HttpClient(tmp_path, refresh=True).get(URL, hosts=HOSTS) is None

    with response_mock([]) as mock:
        assert HttpClient(tmp_path, offline=True).get(URL, hosts=HOSTS) is None
        assert not mock.calls

    with response_mock(f"GET {URL} -> 200 :new"):
        assert HttpClient(tmp_path, refresh=True).get(URL, hosts=HOSTS) == b"new"

    assert not list(tmp_path.rglob("*.404"))


def test_negative_cache_is_partitioned_by_credentials_and_can_be_disabled(
    tmp_path: Path, response_mock: Callable[..., Any],
) -> None:

    with response_mock([f"GET {URL} -> 404 :", f"GET {URL} -> 200 :authorized", f"GET {URL} -> 200 :uncached"]):
        assert HttpClient(tmp_path).get(URL, hosts=HOSTS, headers={"X-Key": "one"}) is None
        assert HttpClient(tmp_path).get(URL, hosts=HOSTS, headers={"X-Key": "two"}) == b"authorized"
        assert HttpClient(tmp_path).get(URL, hosts=HOSTS, headers={"X-Key": "one"}, cache=False) == b"uncached"

    with response_mock([]):
        assert HttpClient(tmp_path).get(URL, hosts=HOSTS, headers={"X-Key": "one"}) is None


def test_404_without_cache_does_not_write_files(tmp_path: Path, response_mock: Callable[..., Any]) -> None:

    with response_mock([f"GET {URL} -> 404 :"] * 2) as mock:
        client = HttpClient(tmp_path)
        assert client.get(URL, hosts=HOSTS, cache=False) is None
        assert HttpClient(tmp_path).get(URL, hosts=HOSTS, cache=False) is None
        assert len(mock.calls) == 2

    assert not list(tmp_path.iterdir())


def test_corrupt_negative_cache_is_not_used(tmp_path: Path, response_mock: Callable[..., Any]) -> None:

    with response_mock(f"GET {URL} -> 404 :"):
        HttpClient(tmp_path).get(URL, hosts=HOSTS)

    next(tmp_path.rglob("*.404")).write_bytes(b"not a status")

    with response_mock(f"GET {URL} -> 200 :found"):
        assert HttpClient(tmp_path).get(URL, hosts=HOSTS) == b"found"
