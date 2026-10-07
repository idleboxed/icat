
import hashlib
import zipfile
from collections.abc import Callable
from ctypes import CDLL
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from icat.errors import CatalogueError
from icat.catalogue.codec import decode
from icat.operations.options import Options
from icat.operations.session import run
from icat.roms.formats import chd_reader


@pytest.mark.parametrize("platform", ["PSX", "SCD"])
@pytest.mark.parametrize("container", ["chd", "zip", "7z"])
@pytest.mark.parametrize("move", [False, True])
def test_sync_publishes_exact_chd_and_handles_repeat_import(
    options: Options, provider: Any, sevenzip_archive: Callable[..., Path], native_chd: CDLL,
    compressed_chd: dict[str, bytes], platform: str, container: str, move: bool,
) -> None:
    source = options.src / f"Game.{container}"
    data = compressed_chd[platform]

    if container == "zip":

        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("nested/Game.chd", data)

    elif container == "7z":
        sevenzip_archive(source, [("nested/Game.chd", data)])

    else:
        source.write_bytes(data)

    result = run(replace(options, move_roms=move), provider)

    entries = decode((options.dst / "catalogue.json").read_bytes())
    digest = hashlib.sha256(data).hexdigest()
    assert (result.imported, result.rejected, result.removed) == (1, 0, int(move))
    assert len(entries) == 1
    assert entries[0]["platform"] == platform
    assert entries[0]["hash"] == digest
    assert entries[0]["image"] == f"{platform}/{digest[:2]}/Game.chd"
    assert (options.dst / "images" / entries[0]["image"]).read_bytes() == data
    assert source.exists() is not move

    repeated = run(options, provider)

    assert repeated.imported == 0
    assert repeated.total == 1

@pytest.mark.parametrize("container", ["chd", "zip", "7z"])
@pytest.mark.parametrize("selected", ["PSX", "SCD"])
def test_platform_filter_does_not_assign_ambiguous_extension_or_remove_excluded_image(
    options: Options, provider: Any, sevenzip_archive: Callable[..., Path], native_chd: CDLL,
    compressed_chd: dict[str, bytes], container: str, selected: str,
) -> None:
    sources = {}

    for platform, data in compressed_chd.items():
        path = options.src / f"{platform}.{container}"

        if container == "zip":

            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("Game.chd", data)

        elif container == "7z":
            sevenzip_archive(path, [("Game.chd", data)])

        else:
            path.write_bytes(data)

        sources[platform] = path

    result = run(replace(options, move_roms=True, platforms=(selected,)), provider)

    assert (result.imported, result.removed, result.rejected) == (1, 1, 0)
    assert not sources[selected].exists()
    assert sources["SCD" if selected == "PSX" else "PSX"].exists()
    assert decode((options.dst / "catalogue.json").read_bytes())[0]["platform"] == selected

@pytest.mark.parametrize("container", ["zip", "7z"])
@pytest.mark.parametrize("extra", ["Game (Disc 2).chd", "Game.sbi", "Game.cue", "Game.m3u"])
def test_multidisc_and_sidecar_archives_are_kept_whole(
    options: Options, provider: Any, sevenzip_archive: Callable[..., Path],
    compressed_chd: dict[str, bytes], container: str, extra: str,
) -> None:
    source = options.src / f"set.{container}"
    entries = [("Game (Disc 1).chd", compressed_chd["PSX"]), (extra, b"component")]

    if container == "zip":

        with zipfile.ZipFile(source, "w") as archive:

            for name, data in entries:
                archive.writestr(name, data)

    else:
        sevenzip_archive(source, entries)

    original = source.read_bytes()

    result = run(replace(options, move_roms=True), provider)

    assert result.imported == result.removed == 0
    assert source.read_bytes() == original

def test_missing_library_stops_sync_and_keeps_source(
    options: Options, provider: Any, chd_image: Callable[..., bytes], monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = options.src / "Game.chd"
    data = chd_image()
    path.write_bytes(data)

    def raise_missing_library() -> None:
        raise CatalogueError("CHD import requires libchdr0")

    monkeypatch.setattr(chd_reader, "load_library", raise_missing_library)

    with pytest.raises(CatalogueError, match="libchdr0"):
        run(replace(options, move_roms=True), provider)

    assert path.read_bytes() == data
    assert not (options.dst / "catalogue.json").exists()
    assert not list(options.dst.glob(".icat-stage-*"))

def test_truncated_chd_source_is_rejected_and_kept(options: Options, provider: Any) -> None:
    path = options.src / "truncated.chd"
    path.write_bytes(b"MComprHD")

    result = run(replace(options, move_roms=True), provider)

    assert (result.imported, result.removed, result.rejected) == (0, 0, 1)
    assert path.read_bytes() == b"MComprHD"
