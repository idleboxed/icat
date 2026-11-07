"""Shared file verification and publication durability boundaries."""

import errno
import hashlib
from io import BytesIO
from pathlib import Path

import pytest

from icat.errors import CatalogueError, SourceError
from icat.io.files import DirectorySync, copy_rom, resolve_relative_file, verify_sha256
from icat.io import files


def test_directory_sync_deduplicates_chains_and_flushes_children_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    monkeypatch.setattr("icat.io.files.sync_directory", calls.append)
    pending = DirectorySync()
    first, second = tmp_path / "images" / "NES" / "aa", tmp_path / "images" / "NES" / "bb"

    for path in (first, second, first):
        pending.add(path, through=tmp_path)

    pending.flush()
    pending.flush()

    assert calls == [first, second, first.parent, first.parent.parent, tmp_path]


def test_invalid_sync_boundary_adds_no_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr("icat.io.files.sync_directory", calls.append)
    pending = DirectorySync()

    with pytest.raises(ValueError, match="not an ancestor"):
        pending.add(tmp_path / "first", through=tmp_path / "second")

    pending.flush()

    assert calls == []


def test_directory_sync_failure_does_not_flush_ancestors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fail(path: Path) -> None:
        calls.append(path)
        raise OSError("Synthetic directory sync failure")

    monkeypatch.setattr("icat.io.files.sync_directory", fail)
    pending = DirectorySync()
    pending.add(tmp_path / "child", through=tmp_path)

    with pytest.raises(OSError, match="sync failure"):
        pending.flush()

    assert calls == [tmp_path / "child"]


def test_sha_verification_checks_content_and_rejects_symlinks(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_bytes(b"synthetic")
    digest = hashlib.sha256(b"synthetic").hexdigest()

    verify_sha256(path, digest, error="Mismatch")
    path.write_bytes(b"corruption")

    with pytest.raises(CatalogueError, match="Mismatch"):
        verify_sha256(path, digest, error="Mismatch")

    link = tmp_path / "link"
    link.symlink_to(path)

    with pytest.raises(OSError, match=rf"\[Errno {errno.ELOOP}\]"):
        verify_sha256(link, digest, error="Mismatch")


@pytest.mark.parametrize("relative, collision", [
    ("SNES/aa/Game.smc", "snes"),
    ("SNES/aa/Game.smc", "SNES/AA"),
    ("SNES/aa/Game.smc", "SNES/aa/game.smc"),
    ("SNES/aa/Straße.smc", "SNES/aa/STRASSE.smc"),
])
def test_relative_resolution_checks_case_collisions_at_every_level(
    tmp_path: Path, relative: str, collision: str,
) -> None:
    image = tmp_path / relative
    image.parent.mkdir(parents=True)
    image.touch()

    assert resolve_relative_file(tmp_path, relative) == image

    conflict = tmp_path / collision
    conflict.touch()

    with pytest.raises(CatalogueError, match="Case-insensitive destination collision") as error:
        resolve_relative_file(tmp_path, relative)

    assert error.value.path == conflict


@pytest.mark.parametrize("linked", ["image", "parent"])
def test_relative_resolution_rechecks_symlinks_between_calls(tmp_path: Path, linked: str) -> None:
    root = tmp_path / "images"
    image = root / "SNES" / "aa" / "Game.smc"
    image.parent.mkdir(parents=True)
    image.touch()
    relative = image.relative_to(root).as_posix()
    assert resolve_relative_file(root, relative) == image
    replaced = image if linked == "image" else image.parent
    outside = tmp_path / "outside"
    replaced.rename(outside)
    replaced.symlink_to(outside)

    with pytest.raises(CatalogueError, match="Symlink destination is not allowed"):
        resolve_relative_file(root, relative)


def test_relative_resolution_propagates_directory_read_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(path: Path) -> list[str]:
        raise OSError(errno.EIO, "Synthetic directory read failure", path)

    monkeypatch.setattr(files.os, "listdir", fail)

    with pytest.raises(OSError, match="Synthetic directory read failure"):
        resolve_relative_file(tmp_path, "Game.smc")


@pytest.mark.parametrize("declared, message", [
    (0, "Empty ROM"), (-1, "Invalid ROM size"), (2, "exceeds declared size"), (4, "Incomplete ROM copy"),
])
def test_rom_copy_rejects_declared_size_mismatch(tmp_path: Path, declared: int, message: str) -> None:

    with pytest.raises(SourceError, match=message):
        copy_rom(BytesIO(b"ROM"), tmp_path / "rom", expected_size=declared)


def test_rom_copy_streams_and_syncs_only_after_complete_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(files, "CHUNK", 2)
    monkeypatch.setattr(files.os, "fsync", calls.append)
    path = tmp_path / "rom"

    copy_rom(BytesIO(b"synthetic"), path, expected_size=9)

    assert path.read_bytes() == b"synthetic"
    assert len(calls) == 1
