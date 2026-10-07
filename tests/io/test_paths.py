"""Filesystem layout overrides, using synthetic ROMs and offline providers only.

SPDX-License-Identifier: BSD-3-Clause
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from icat.catalogue.codec import decode
from icat.metadata.types import Metadata
from icat.roms.types import Rom
from icat.operations.options import Options
from icat.operations.result import Result
from icat.operations.session import run
from icat.io import paths
from icat import cli


@pytest.mark.parametrize("xdg", [None, "xdg-cache"])
def test_cache_default_resolves_current_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, xdg: str | None,
) -> None:
    monkeypatch.setenv("HOME", f"{tmp_path / "home"}")
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)

    if xdg is not None:
        monkeypatch.setenv("XDG_CACHE_HOME", f"{tmp_path / xdg}")

    result = paths.get_default_cache_dir()
    monkeypatch.setenv("XDG_CACHE_HOME", f"{tmp_path / "next-cache"}")
    next_result = paths.get_default_cache_dir()

    base = tmp_path / xdg if xdg is not None else tmp_path / "home" / ".cache"
    assert result == base / "icat"
    assert next_result == tmp_path / "next-cache" / "icat"


@pytest.mark.parametrize(("flags", "source_name"), [([], "roms"), (["--src", "rom"], "rom")])
def test_cli_imports_from_default_or_explicit_source_in_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nes: bytes, flags: list[str], source_name: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", f"{tmp_path / "cache"}")
    source = tmp_path / source_name
    source.mkdir()
    original = source / "Game.nes"
    original.write_bytes(nes)

    result = cli.main(["sync", "--http-offline", *flags])

    assert result == 0
    entries = decode((tmp_path / "games" / "catalogue.json").read_bytes())
    assert len(entries) == 1
    assert (tmp_path / "games" / "images" / entries[0]["image"]).read_bytes() == nes
    assert original.read_bytes() == nes
    assert len(list((tmp_path / "logs" / "sync").glob("*/journal.json"))) == 1
    assert not list((tmp_path / "cache" / "icat").rglob("journal.json"))


def test_cli_help_shows_roms_as_default_source(capsys: pytest.CaptureFixture[str]) -> None:

    with pytest.raises(SystemExit) as exc:
        cli.main(["sync", "--help"])

    assert exc.value.code == 0
    assert "Source directory (default: roms)" in capsys.readouterr().out


def test_cli_uses_central_defaults_and_explicit_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(paths, "SOURCE_DIR", tmp_path / "input")
    monkeypatch.setattr(paths, "GAMES_DIR", tmp_path / "library")
    monkeypatch.setenv("XDG_CACHE_HOME", f"{tmp_path / "cache"}")
    calls = []

    def capture(options: Options, provider: object) -> Result:
        calls.append(options)
        return Result(0, 0, 0, 0, 0)

    monkeypatch.setattr(cli, "run", capture)

    assert cli.main(["sync", "--http-offline"]) == 0
    assert cli.main([
        "sync", "--http-offline", "--src", "custom-in", "--dst", "custom-out",
        "--cache", "custom-cache", "--logs_dir", "custom-logs",
    ]) == 0

    assert (calls[0].src, calls[0].dst, calls[0].cache, calls[0].logs) == (
        tmp_path / "input",
        tmp_path / "library",
        tmp_path / "cache" / "icat",
        tmp_path / "logs",
    )
    assert tuple(f"{path}" for path in (calls[1].src, calls[1].dst, calls[1].cache, calls[1].logs)) == (
        "custom-in", "custom-out", "custom-cache", "custom-logs",
    )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ([], (False, False, False)),
        (["--move-roms"], (True, False, False)),
        (["--trash"], (False, True, False)),
        (["--trash-reset"], (False, False, True)),
        (["--move-roms", "--trash"], (True, True, False)),
    ],
)
def test_cli_passes_explicit_move_and_trash_flags(
    monkeypatch: pytest.MonkeyPatch, flags: list[str], expected: tuple[bool, bool, bool],
) -> None:
    calls = []

    def capture(options: Options, provider: object) -> Result:
        calls.append(options)
        return Result(0, 0, 0, 0, 0)

    monkeypatch.setattr(cli, "run", capture)

    assert cli.main(["sync", "--http-offline", *flags]) == 0

    options = calls[0]
    assert (options.move_roms, options.trash, options.trash_reset) == expected


@pytest.mark.parametrize("flags", [["--copy"], ["--max-rom-mib", "128"], ["--trash", "--trash-reset"]])
def test_cli_rejects_removed_or_conflicting_flags(flags: list[str]) -> None:

    with pytest.raises(SystemExit) as exc:
        cli.main(["sync", *flags])

    assert exc.value.code == 2


def test_sync_reuses_renamed_layout_for_publish_readback_and_cleanup(
    options: Options, nes: bytes, png: bytes, monkeypatch: pytest.MonkeyPatch,
) -> None:

    for name, value in {
        "CATALOGUE_FILE": "index.json",
        "IMAGES_DIR": "cartridges",
        "THUMBS_DIR": "previews",
        "JOURNAL_FILE": "report.json",
        "LOCK_FILE": ".import.lock",
        "STAGING_PREFIX": ".import-stage-",
    }.items():
        monkeypatch.setattr(paths, name, value)

    source = options.src / "Synthetic.nes"
    source.write_bytes(nes)

    class Provider:
        def lookup(self, rom: Rom) -> Metadata:
            return Metadata(thumbnail=png)

    first = run(options, Provider())
    second = run(replace(options, move_roms=True), Provider())

    entry = decode((options.dst / "index.json").read_bytes())[0]
    assert first.imported == second.total == second.removed == 1
    assert second.imported == second.missing_thumbnails == 0
    assert not source.exists()
    assert (options.dst / "cartridges" / entry["image"]).read_bytes() == nes
    assert (options.dst / "previews" / paths.get_thumbnail_name(entry["hash"])).read_bytes() == png
    assert {path.name for path in options.dst.iterdir()} == {"index.json", "cartridges", "previews"}
    reports = list(options.logs.rglob("report.json"))
    assert len(reports) == 2
    assert all(json.loads(path.read_bytes())["state"] == "complete" for path in reports)
