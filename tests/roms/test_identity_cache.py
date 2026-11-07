import json
import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from icat.catalogue.codec import create_empty_entry
from icat.errors import CatalogueError
from icat.roms import identity_cache
from icat.roms.identity_cache import IdentityCache
from icat.roms.inspection import inspect_rom
from icat.roms.types import Rom

def test_payload_identity_roundtrip_uses_new_path_and_independent_metadata(tmp_path: Path, rom: Rom) -> None:
    root = tmp_path / "cache"
    cache = IdentityCache(root)
    rom.entry["region"] = ["Japan"]
    rom.has_thumbnail = True
    cache.put(rom)
    assert not root.exists()

    cache.save()
    rom.path.unlink()
    path = tmp_path / "Renamed.NES"
    entry = {**rom.entry, "image": path.name, "title": "New title"}
    restored_cache = IdentityCache(root)

    restored = restored_cache.get(path, entry)

    assert restored is not None
    assert restored.path == path
    assert restored.entry == entry
    assert restored.entry is not entry
    assert restored.size == rom.size == rom.full_lookup[1]
    assert restored.lookups == rom.lookups
    assert restored.full_lookup == rom.full_lookup
    assert restored.crc32s == rom.crc32s
    assert not restored.has_thumbnail
    restored.entry["region"].append("USA")
    restored.lookups.clear()
    restored.crc32s.clear()
    assert entry["region"] == ["Japan"]
    again = restored_cache.get(path, entry)
    assert again.lookups == rom.lookups
    assert again.crc32s == rom.crc32s
    assert again.entry["region"] == ["Japan"]

@pytest.mark.parametrize("kind", ["snes", "smd", "v64", "n64"])
def test_normalized_lookup_order_crc_and_physical_size_roundtrip(
    tmp_path: Path, lookup_image: Callable[[str], tuple[str, str, bytes, bytes]], kind: str,
) -> None:
    _platform, suffix, _canonical, encoded = lookup_image(kind)
    path = tmp_path / f"Game{suffix}"
    path.write_bytes(encoded)
    rom = inspect_rom(path, path.name)
    cache = IdentityCache(tmp_path / "cache")
    cache.put(rom)
    cache.save()

    restored = IdentityCache(cache.root).get(path, rom.entry)

    assert len(rom.lookups) == 2
    assert restored is not None
    assert restored.size == len(encoded)
    assert restored.lookups == rom.lookups
    assert restored.full_lookup == rom.full_lookup
    assert restored.crc32s == rom.crc32s

@pytest.mark.parametrize("platform", ["PSX", "SCD"])
def test_chd_container_does_not_acquire_cartridge_lookups(tmp_path: Path, platform: str) -> None:
    path = tmp_path / "Disc.chd"
    rom = Rom(path, create_empty_entry(path.name, "a" * 64, platform), [], size=12345)
    cache = IdentityCache(tmp_path / "cache")
    cache.put(rom)
    cache.save()

    restored = IdentityCache(cache.root).get(path, rom.entry)

    assert restored is not None
    assert restored.size == 12345
    assert restored.lookups == []
    assert restored.full_lookup is None
    assert restored.crc32s == {}
    assert not path.exists()

@pytest.mark.parametrize("platform, suffix", [("NES", ".nes"), ("PSX", ".chd"), ("SCD", ".chd")])
def test_extensionless_staged_rom_uses_logical_image_name(
    tmp_path: Path, nes: bytes, platform: str, suffix: str,
) -> None:
    staged_path = tmp_path / "staging" / "0"
    name = f"Game{suffix}"

    if platform == "NES":
        staged_path.parent.mkdir()
        staged_path.write_bytes(nes)
        rom = inspect_rom(staged_path, name)

    else:
        rom = Rom(staged_path, create_empty_entry(name, "a" * 64, platform), [], size=12345)

    cache = IdentityCache(tmp_path / "cache")
    cache.put(rom)
    cache.save()
    entry = {**rom.entry, "image": f"{platform}/ab/{name}"}
    target = tmp_path / "games" / "images" / entry["image"]

    restored = IdentityCache(cache.root).get(target, entry)

    assert restored is not None
    assert restored.path == target
    assert restored.entry == entry
    assert restored.size == rom.size
    assert restored.lookups == rom.lookups
    assert restored.crc32s == rom.crc32s
    assert restored.full_lookup == rom.full_lookup

def test_suffix_and_platform_are_not_interchangeable(tmp_path: Path, nes: bytes, rom: Rom) -> None:
    path = tmp_path / "Game.unf"
    path.write_bytes(nes)
    alternate = inspect_rom(path, path.name)
    cache = IdentityCache(tmp_path / "cache")
    cache.put(rom)
    cache.save()
    # Saving a new identity before the first get must retain earlier cached data.
    changed_cache = IdentityCache(cache.root)
    changed_cache.put(alternate)
    changed_cache.save()
    restored_cache = IdentityCache(cache.root)

    restored = restored_cache.get(rom.path, rom.entry)
    restored_alternate = restored_cache.get(alternate.path, alternate.entry)

    assert alternate.entry["hash"] == rom.entry["hash"]
    assert restored.lookups == rom.lookups
    assert restored_alternate.lookups == alternate.lookups
    assert restored_alternate.lookups != restored.lookups
    assert restored_cache.get(rom.path, {**rom.entry, "platform": "SNES"}) is None
    assert restored_cache.get(rom.path, {**rom.entry, "hash": "0" * 64}) is None

def test_constructor_put_and_unchanged_save_do_not_access_files(
    tmp_path: Path, rom: Rom, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_io(*args: object, **kwargs: object) -> None:
        raise AssertionError("Unexpected filesystem access")

    for name in ("get_file_stamp", "read_bytes", "resolve_relative_file", "ensure_directory", "write_atomic"):
        monkeypatch.setattr(identity_cache, name, reject_io)

    cache = IdentityCache(tmp_path / "cache")
    cache.save()
    cache.put(rom)

    assert not cache.root.exists()

def test_index_is_loaded_once_and_equal_put_does_not_rewrite(
    tmp_path: Path, rom: Rom, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = IdentityCache(tmp_path / "cache")
    cache.put(rom)
    cache.save()
    reads = []
    read_bytes = identity_cache.read_bytes

    def read_once(path: Path, limit: int) -> bytes:
        reads.append(path)
        return read_bytes(path, limit)

    def reject_write(path: Path, data: bytes) -> None:
        raise AssertionError("Unchanged identity cache must not be rewritten")

    monkeypatch.setattr(identity_cache, "read_bytes", read_once)
    monkeypatch.setattr(identity_cache, "write_atomic", reject_write)
    restored_cache = IdentityCache(cache.root)
    restored_cache.put(rom)
    restored_cache.save()
    assert restored_cache.get(rom.path, rom.entry) is not None
    assert restored_cache.get(rom.path, rom.entry) is not None
    restored_cache.put(rom)
    restored_cache.save()

    assert reads == [cache.root / IdentityCache.FILE_NAME]

@pytest.mark.parametrize("body", [
    pytest.param(b"not JSON", id="malformed"),
    pytest.param(b"\xff", id="encoding"),
    pytest.param(b"[]", id="array"),
    pytest.param(b"null", id="null"),
    pytest.param(b'{"same":{},"same":{}}', id="duplicate"),
    pytest.param(b"[" * 1100 + b"]" * 1100, id="deeply-nested"),
])
def test_malformed_index_is_a_reported_cache_miss(
    tmp_path: Path, rom: Rom, caplog: pytest.LogCaptureFixture, body: bytes,
) -> None:
    path = tmp_path / IdentityCache.FILE_NAME
    path.write_bytes(body)

    restored = IdentityCache(tmp_path).get(rom.path, rom.entry)

    assert restored is None
    assert "invalid identity cache; ignored" in caplog.text
    assert path.read_bytes() == body

@pytest.mark.parametrize("field, value", [
    pytest.param("size", 0, id="empty"),
    pytest.param("size", True, id="boolean-size"),
    pytest.param("size", 2**63, id="oversized-integer"),
    pytest.param("platform", "SNES", id="wrong-platform"),
    pytest.param("platform", "UNKNOWN", id="unknown-platform"),
    pytest.param("lookups", [], id="missing-lookup"),
    pytest.param("lookups", [["a" * 40, True, "0" * 8]], id="boolean-lookup-size"),
    pytest.param("lookups", [["invalid", 1, "0" * 8]], id="invalid-sha1"),
    pytest.param("lookups", [["a" * 40, 1, "invalid"]], id="invalid-crc"),
    pytest.param("lookups", [["a" * 40, 2**40, "0" * 8]], id="lookup-beyond-file"),
    pytest.param("full_lookup", None, id="missing-full-lookup"),
    pytest.param("full_lookup", ["b" * 40, 1], id="foreign-full-lookup"),
    pytest.param("extra", True, id="unknown-field"),
])
def test_invalid_records_cannot_restore_an_identity(
    tmp_path: Path, rom: Rom, caplog: pytest.LogCaptureFixture, field: str, value: object,
) -> None:
    cache = IdentityCache(tmp_path)
    cache.put(rom)
    cache.save()
    path = tmp_path / IdentityCache.FILE_NAME
    document = json.loads(path.read_bytes())
    record = next(iter(document.values()))
    record[field] = value
    path.write_text(json.dumps(document), encoding="utf-8")

    restored = IdentityCache(tmp_path).get(rom.path, rom.entry)

    assert restored is None
    assert "invalid identity cache; ignored" in caplog.text

@pytest.mark.parametrize("changes", [
    pytest.param({"size": None}, id="unknown-size"),
    pytest.param({"size": 0}, id="empty"),
    pytest.param({"size": True}, id="boolean-size"),
    pytest.param({"lookups": []}, id="no-lookups"),
    pytest.param({"crc32s": {}}, id="no-crcs"),
    pytest.param({"full_lookup": None}, id="no-full-lookup"),
])
def test_incomplete_roms_are_not_persisted(tmp_path: Path, rom: Rom, changes: dict) -> None:
    cache = IdentityCache(tmp_path / "cache")

    cache.put(replace(rom, **changes))
    cache.save()

    assert cache.get(rom.path, rom.entry) is None
    assert not cache.root.exists()

@pytest.mark.parametrize("key", [
    pytest.param("a" * 63 + ".nes", id="truncated-sha256"),
    pytest.param("a" * 65 + ".nes", id="extended-sha256"),
    pytest.param("A" * 64 + ".nes", id="uppercase-sha256"),
    pytest.param("a" * 64 + ".zip", id="unsupported-suffix"),
])
def test_invalid_record_keys_are_ignored(
    tmp_path: Path, rom: Rom, caplog: pytest.LogCaptureFixture, key: str,
) -> None:
    cache = IdentityCache(tmp_path)
    cache.put(rom)
    cache.save()
    path = tmp_path / IdentityCache.FILE_NAME
    record = next(iter(json.loads(path.read_bytes()).values()))
    path.write_text(json.dumps({key: record}), encoding="utf-8")

    assert IdentityCache(tmp_path).get(rom.path, rom.entry) is None

    assert "invalid identity cache; ignored" in caplog.text

def test_bounded_index_keeps_newest_identities(
    tmp_path: Path, rom: Rom, nes: bytes, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    cache = IdentityCache(tmp_path / "cache")
    cache.put(rom)
    cache.save()
    path = cache.root / IdentityCache.FILE_NAME
    limit = path.stat().st_size
    monkeypatch.setattr(identity_cache, "LIMIT", limit)
    alternate_path = tmp_path / "Game.unf"
    alternate_path.write_bytes(nes)
    alternate = inspect_rom(alternate_path, alternate_path.name)
    cache.put(alternate)

    cache.save()

    assert path.stat().st_size <= limit
    restored_cache = IdentityCache(cache.root)
    assert restored_cache.get(alternate.path, alternate.entry) is not None
    assert restored_cache.get(rom.path, rom.entry) is None
    assert "dropped 1 older identities" in caplog.text

def test_oversized_regular_index_is_a_reported_miss(
    tmp_path: Path, rom: Rom, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / IdentityCache.FILE_NAME
    path.write_bytes(b" " * 11)
    monkeypatch.setattr(identity_cache, "LIMIT", 10)

    assert IdentityCache(tmp_path).get(rom.path, rom.entry) is None

    assert "identity cache exceeds size limit; ignored" in caplog.text

@pytest.mark.parametrize("location", ["file", "root", "ancestor"])
def test_symlinked_cache_paths_are_fatal(tmp_path: Path, rom: Rom, location: str) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"keep")
    root = tmp_path / "cache"

    if location == "file":
        root.mkdir()
        (root / IdentityCache.FILE_NAME).symlink_to(sentinel)

    elif location == "root":
        root.symlink_to(outside, target_is_directory=True)

    else:
        root.symlink_to(outside, target_is_directory=True)
        root = root / "nested"

    with pytest.raises(CatalogueError, match="Symlink"):
        IdentityCache(root).get(rom.path, rom.entry)

    assert sentinel.read_bytes() == b"keep"
    assert sorted(outside.iterdir()) == [sentinel]

@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_special_cache_files_are_fatal(tmp_path: Path, rom: Rom, kind: str) -> None:
    path = tmp_path / IdentityCache.FILE_NAME

    if kind == "directory":
        path.mkdir()

    else:
        os.mkfifo(path)

    with pytest.raises(CatalogueError, match="Not a regular file"):
        IdentityCache(tmp_path).get(rom.path, rom.entry)

def test_cache_read_errors_are_not_disguised_as_a_miss(
    tmp_path: Path, rom: Rom, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / IdentityCache.FILE_NAME).write_bytes(b"{}\n")

    def reject_read(path: Path, limit: int) -> bytes:
        raise PermissionError("Unreadable identity cache")

    monkeypatch.setattr(identity_cache, "read_bytes", reject_read)

    with pytest.raises(PermissionError, match="Unreadable identity cache"):
        IdentityCache(tmp_path).get(rom.path, rom.entry)

@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_cache_file_replacement_before_save_is_rejected(tmp_path: Path, rom: Rom, kind: str) -> None:
    cache = IdentityCache(tmp_path)
    assert cache.get(rom.path, rom.entry) is None
    cache.put(rom)
    path = tmp_path / IdentityCache.FILE_NAME
    outside = tmp_path / "outside"
    outside.write_bytes(b"keep")

    if kind == "symlink":
        path.symlink_to(outside)

    elif kind == "directory":
        path.mkdir()

    else:
        os.mkfifo(path)

    with pytest.raises(CatalogueError, match="Symlink|Not a regular file"):
        cache.save()

    assert outside.read_bytes() == b"keep"

def test_cache_root_replacement_before_save_is_rejected(tmp_path: Path, rom: Rom) -> None:
    root = tmp_path / "cache"
    cache = IdentityCache(root)
    assert cache.get(rom.path, rom.entry) is None
    cache.put(rom)
    outside = tmp_path / "outside"
    outside.mkdir()
    root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(CatalogueError, match="Symlink"):
        cache.save()

    assert not list(outside.iterdir())

def test_failed_atomic_save_preserves_old_index_and_allows_retry(
    tmp_path: Path, rom: Rom, nes: bytes, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = IdentityCache(tmp_path / "cache")
    cache.put(rom)
    cache.save()
    path = cache.root / IdentityCache.FILE_NAME
    original = path.read_bytes()
    alternate_path = tmp_path / "Game.unf"
    alternate_path.write_bytes(nes)
    alternate = inspect_rom(alternate_path, alternate_path.name)
    cache.put(alternate)

    def reject_replace(source: str | Path, destination: str | Path) -> None:
        raise OSError("Identity cache publication failed")

    with monkeypatch.context() as context:
        context.setattr("icat.io.files.os.replace", reject_replace)

        with pytest.raises(OSError, match="Identity cache publication failed"):
            cache.save()

    assert path.read_bytes() == original
    assert sorted(cache.root.iterdir()) == [path]
    cache.save()
    assert IdentityCache(cache.root).get(alternate.path, alternate.entry) is not None
