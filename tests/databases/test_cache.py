import json
import warnings
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from icat.databases.cache import DatasetCache
from icat.metadata.provider import Provider
from icat.io.http import HttpClient
from icat.roms.types import Rom
from icat.metadata.types import Metadata
from icat.metadata.sources.openvgdb import URL, OpenVgdbSource
from icat.databases import cache as datasets


def test_dataset_downloaded_once_for_workers_then_persisted_across_runs(
    source: OpenVgdbSource, tmp_path: Path, rom: Rom,
    openvgdb_archive: Callable[..., bytes], response_mock: Callable[..., Any],
) -> None:
    def lookup(_number: int) -> Metadata:
        return Provider([source]).lookup(rom)

    with response_mock(f"GET {URL} -> 200 :".encode() + openvgdb_archive()) as mock:

        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lookup, range(10)))

        assert all(result.fields["year"] == 1991 for result in results)
        assert len(mock.calls) == 1

    for offline in (False, True):
        # HTTP --http-refresh must not download databases again; only --refresh-db does that.
        again = OpenVgdbSource(
            HttpClient(tmp_path / "cache", offline=offline, refresh=True),
            DatasetCache(tmp_path / "datasets", offline=offline),
        )

        with response_mock([]) as mock:
            assert Provider([again]).lookup(rom).fields["year"] == 1991
            assert not mock.calls

@pytest.mark.parametrize("body", [b"not a ZIP", b"", None])
def test_failed_dataset_refresh_preserves_previous_validated_version(
    source: OpenVgdbSource, tmp_path: Path, rom: Rom,
    openvgdb_archive: Callable[..., bytes], response_mock: Callable[..., Any], body: bytes | None,
) -> None:

    with response_mock(f"GET {URL} -> 200 :".encode() + openvgdb_archive()):
        Provider([source]).lookup(rom)

    manifest = tmp_path / "datasets/openvgdb-v29/current.json"
    original = manifest.read_bytes()
    refreshed = OpenVgdbSource(source.http, DatasetCache(tmp_path / "datasets", refresh=True))
    rule = f"GET {URL} -> 500 :".encode() if body is None else f"GET {URL} -> 200 :".encode() + body

    with response_mock(rule):
        assert Provider([refreshed]).lookup(rom).fields["year"] == 1991

    assert manifest.read_bytes() == original

def test_successful_dataset_refresh_publishes_new_version_without_deleting_old(
    source: OpenVgdbSource, tmp_path: Path, rom: Rom,
    openvgdb_archive: Callable[..., bytes], response_mock: Callable[..., Any],
) -> None:

    with response_mock(f"GET {URL} -> 200 :".encode() + openvgdb_archive()):
        Provider([source]).lookup(rom)

    manifest = tmp_path / "datasets/openvgdb-v29/current.json"
    previous = json.loads(manifest.read_text())["sha256"]
    archive = openvgdb_archive(releases=[(13, "Game", "New description", "Action", "1992")])
    refreshed = OpenVgdbSource(source.http, DatasetCache(tmp_path / "datasets", refresh=True))

    with response_mock(f"GET {URL} -> 200 :".encode() + archive):
        assert Provider([refreshed]).lookup(rom).fields["year"] == 1992

    assert json.loads(manifest.read_text())["sha256"] != previous
    assert (manifest.parent / previous).is_file()

def test_corrupted_cached_database_is_not_read_offline(
    source: OpenVgdbSource, tmp_path: Path, rom: Rom,
    openvgdb_archive: Callable[..., bytes], response_mock: Callable[..., Any],
) -> None:

    with response_mock(f"GET {URL} -> 200 :".encode() + openvgdb_archive()):
        Provider([source]).lookup(rom)

    manifest = tmp_path / "datasets/openvgdb-v29/current.json"
    (manifest.parent / json.loads(manifest.read_text())["sha256"]).write_bytes(b"corrupt")
    offline = OpenVgdbSource(
        HttpClient(tmp_path / "cache", offline=True), DatasetCache(tmp_path / "datasets", offline=True)
    )

    with response_mock([]) as mock:
        assert Provider([offline]).lookup(rom).fields == {}
        assert not mock.calls

def test_empty_offline_cache_does_not_use_network(
    source: OpenVgdbSource, tmp_path: Path, rom: Rom, response_mock: Callable[..., Any],
) -> None:
    offline = OpenVgdbSource(
        HttpClient(tmp_path / "cache", offline=True), DatasetCache(tmp_path / "datasets", offline=True)
    )

    with response_mock([]) as mock:
        assert Provider([offline]).lookup(rom).fields == {}
        assert not mock.calls

def test_interrupted_dataset_update_leaves_manifest_and_sources_intact(
    source: OpenVgdbSource, tmp_path: Path, rom: Rom, openvgdb_archive: Callable[..., bytes],
    response_mock: Callable[..., Any], monkeypatch: pytest.MonkeyPatch,
) -> None:

    with response_mock(f"GET {URL} -> 200 :".encode() + openvgdb_archive()):
        Provider([source]).lookup(rom)

    manifest = tmp_path / "datasets/openvgdb-v29/current.json"
    original = manifest.read_bytes()
    original_write = datasets.write_atomic
    refreshed = OpenVgdbSource(source.http, DatasetCache(tmp_path / "datasets", refresh=True))

    def interrupt_manifest(path: Path, data: bytes) -> None:

        if path.name == "current.json":
            raise KeyboardInterrupt

        original_write(path, data)

    monkeypatch.setattr(datasets, "write_atomic", interrupt_manifest)

    with response_mock(f"GET {URL} -> 200 :".encode() + openvgdb_archive()):

        with pytest.raises(KeyboardInterrupt, match="^$"):
            Provider([refreshed]).lookup(rom)

    assert manifest.read_bytes() == original
    assert rom.path.is_file()

def test_database_cache_refuses_symlinked_directories(
    tmp_path: Path, rom: Rom, response_mock: Callable[..., Any],
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "datasets").symlink_to(outside, target_is_directory=True)
    source = OpenVgdbSource(HttpClient(tmp_path / "cache"), DatasetCache(tmp_path / "datasets"))

    with response_mock([]) as mock:
        assert Provider([source]).lookup(rom).fields == {}
        assert not mock.calls

    assert not list(outside.iterdir())

@pytest.mark.parametrize("kind", ["wrong-name", "duplicate", "bad-schema"])
def test_untrusted_database_archives_are_rejected_before_publication(
    source: OpenVgdbSource, tmp_path: Path, rom: Rom, response_mock: Callable[..., Any], kind: str,
) -> None:
    stream = BytesIO()

    with warnings.catch_warnings(), zipfile.ZipFile(stream, "w") as archive:
        warnings.simplefilter("ignore", UserWarning)
        name = "../openvgdb.sqlite" if kind == "wrong-name" else "openvgdb.sqlite"
        archive.writestr(name, b"not a SQLite database")

        if kind == "duplicate":
            archive.writestr(name, b"another payload")

    with response_mock(f"GET {URL} -> 200 :".encode() + stream.getvalue()):
        assert Provider([source]).lookup(rom).fields == {}

    assert not (tmp_path / "datasets/openvgdb-v29/current.json").exists()
    assert not (tmp_path / "datasets/openvgdb.sqlite").exists()
