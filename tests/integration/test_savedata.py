"""Persistent saves are not catalogue assets; synthetic data, no hardware.

SPDX-License-Identifier: BSD-3-Clause
"""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from icat.catalogue.codec import decode
from icat.operations.options import Options
from icat.operations.session import run


SavedLibrary = tuple[dict, dict[Path, tuple[bytes, int, int]]]


@pytest.fixture
def saved_library(options: Options, nes: bytes, provider: Any) -> SavedLibrary:
    (options.src / "Original.nes").write_bytes(nes)
    run(options, provider)
    entry = decode((options.dst / "catalogue.json").read_bytes())[0]
    digest = entry["hash"]
    files = {}

    for directory, names in (
        ("states", (f"{digest}.state", f"{digest}.state3", "unindexed.state")),
        ("saves", (f"{digest}.srm", f"{digest}.rtc", f"{digest}.sav", "unindexed.srm")),
    ):
        root = options.dst / directory
        root.mkdir()

        for name in names:
            path = root / name
            data = f"Synthetic persistent {name}".encode()
            path.write_bytes(data)
            files[path] = (data, path.stat().st_ino, path.stat().st_mtime_ns)

    return entry, files


@pytest.mark.parametrize("mode", ["import", "metadata", "clean", "reset", "sync-clean", "sync-reset"])
def test_catalogue_operations_preserve_both_save_directories(
    options: Options, provider: Any, saved_library: SavedLibrary, mode: str,
) -> None:
    entry, files = saved_library
    (options.dst.parent / "prefs.json").write_text(json.dumps({"trash": [entry["hash"]]}))
    operation = replace(
        options,
        src=None if mode in ("metadata", "clean", "reset") else options.src,
        trash=mode in ("clean", "sync-clean"),
        trash_reset=mode in ("reset", "sync-reset"),
        trash_only=mode in ("clean", "reset"),
    )

    run(operation, provider)

    for path, (data, inode, modified) in files.items():
        assert path.read_bytes() == data
        assert path.stat().st_ino == inode
        assert path.stat().st_mtime_ns == modified

    if mode == "clean":
        assert decode((options.dst / "catalogue.json").read_bytes()) == []
        assert not (options.dst / "images" / entry["image"]).exists()


def test_renamed_and_reimported_rom_keeps_hash_and_saves(
    options: Options, provider: Any, saved_library: SavedLibrary,
) -> None:
    entry, files = saved_library
    (options.src / "Original.nes").rename(options.src / "Renamed.nes")
    (options.dst.parent / "prefs.json").write_text(json.dumps({"trash": [entry["hash"]]}))

    run(replace(options, src=None, trash=True, trash_only=True), provider)
    result = run(options, provider)

    restored = decode((options.dst / "catalogue.json").read_bytes())[0]
    assert result.imported == 1
    assert restored["hash"] == entry["hash"]
    assert restored["image"] != entry["image"]
    assert all(path.read_bytes() == data for path, (data, _inode, _modified) in files.items())


def test_stored_placeholder_keeps_rom_and_persistent_saves(
    options: Options, provider: Any, saved_library: SavedLibrary,
) -> None:
    entry, files = saved_library
    catalogue = options.dst / "catalogue.json"
    document = json.loads(catalogue.read_text())
    document["games"][0]["title"] = "ZZZ"
    catalogue.write_text(json.dumps(document))

    result = run(replace(options, src=None), provider)

    assert result.total == 1
    assert result.excluded == 0
    assert decode(catalogue.read_bytes())[0]["title"] == "Original"
    assert (options.dst / "images" / entry["image"]).exists()
    assert all(path.read_bytes() == data for path, (data, _inode, _modified) in files.items())
