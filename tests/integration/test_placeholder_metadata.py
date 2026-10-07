"""Placeholder metadata never excludes ROMs or authorizes destination cleanup.

SPDX-License-Identifier: BSD-3-Clause
"""

import hashlib
import json
import zipfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from icat.errors import CatalogueError
from icat.catalogue.codec import decode, encode
from icat.metadata.types import Metadata
from icat.metadata.provider import build_provider
from icat.io.http import HttpClient
from icat.io.paths import get_thumbnail_name
from icat.roms.types import Rom
from icat.operations.options import Options
from icat.operations.session import run


@pytest.fixture
def placeholder_rule(lookup_response: dict) -> str:
    lookup_response["name"] = "ZZZ"
    lookup_response["signature"]["game"]["name"] = "ZZZ(notgame):#NONGAME"
    digest = lookup_response["signature"]["rom"]["sha1"]
    return f"GET https://hasheous.org/api/v1/Lookup/ByHash/sha1/{digest} -> 200 :{json.dumps(lookup_response)}"


@pytest.mark.parametrize("container", ["nes", "zip", "7z"])
@pytest.mark.parametrize("move", [False, True])
def test_placeholder_rom_is_imported_with_normal_copy_move_rules(
    options: Options, nes: bytes, sevenzip_archive: Callable[..., Path], placeholder_rule: str,
    response_mock: Callable[..., Any], caplog: pytest.LogCaptureFixture, container: str, move: bool,
) -> None:
    path = options.src / f"Game.{container}"

    if container == "zip":

        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("Game.nes", nes)
            archive.writestr("Game (U).nes", nes[:-1] + b"D")

    elif container == "7z":
        sevenzip_archive(path, [("Game.nes", nes), ("Game (U).nes", nes[:-1] + b"D")])

    else:
        path.write_bytes(nes)

    original = path.read_bytes()
    pipeline = build_provider(HttpClient(options.cache), environ={}, source_names=["hasheous"])

    with response_mock(placeholder_rule):
        result = run(replace(options, move_roms=move), pipeline)

    assert result.imported == result.total == 1
    assert result.removed == int(move)
    assert result.excluded == result.excluded_files_removed == result.rejected == 0
    entry = decode((options.dst / "catalogue.json").read_bytes())[0]
    assert entry["title"] == "Game"
    assert entry["hash"] == hashlib.sha256(nes).hexdigest()
    assert entry["description"] is None
    assert entry["year"] is None
    assert (options.dst / "images" / entry["image"]).read_bytes() == nes

    if move:
        assert not path.exists()

    else:
        assert path.read_bytes() == original

    assert "ZZZ metadata from hasheous ignored" in caplog.text
    journal = json.loads(next(options.logs.rglob("journal.json")).read_text())
    assert journal["excluded_games"] == journal["excluded_removed"] == journal["skipped_sources"] == []
    assert journal["summary"]["excluded"] == journal["summary"]["excluded_files_removed"] == 0
    assert journal["diagnostics"]["counts"]["hasheous"]["metadata.ignored_placeholder"] == 1


@pytest.fixture
def stored_game(options: Options, nes: bytes, png: bytes) -> tuple[dict, Path, Path]:
    (options.src / "Game.nes").write_bytes(nes)

    class Initial:
        def lookup(self, rom: Rom) -> Metadata:
            return Metadata(thumbnail=png)

    run(replace(options, move_roms=True), Initial())
    catalogue = options.dst / "catalogue.json"
    entry = decode(catalogue.read_bytes())[0]
    image = options.dst / "images" / entry["image"]
    thumbnail = options.dst / "thumbs" / get_thumbnail_name(entry["hash"])
    return entry, image, thumbnail


@pytest.mark.parametrize("stored_title", ["ZZZ", " zzz ", "Manual title"])
@pytest.mark.parametrize("metadata_only", [False, True])
def test_existing_rom_thumbnail_and_preferences_survive_placeholder_response(
    options: Options, stored_game: tuple[dict, Path, Path], nes: bytes, png: bytes, placeholder_rule: str,
    response_mock: Callable[..., Any], stored_title: str, metadata_only: bool,
) -> None:
    entry, image, thumbnail = stored_game
    catalogue = options.dst / "catalogue.json"
    catalogue.write_bytes(encode([{**entry, "title": stored_title}]))
    preferences = options.dst.parent / "prefs.json"
    preferences.write_text("{\"custom\":true}")
    original_stats = [(path.stat().st_ino, path.stat().st_mtime_ns) for path in (image, thumbnail)]
    pipeline = build_provider(HttpClient(options.cache), environ={}, source_names=["hasheous"])

    with response_mock(placeholder_rule):
        result = run(replace(options, src=None) if metadata_only else options, pipeline)

    assert result.total == 1 and result.imported == result.removed == result.excluded == 0
    expected = "Manual title" if stored_title == "Manual title" else "Game"
    assert decode(catalogue.read_bytes())[0]["title"] == expected
    assert image.read_bytes() == nes and thumbnail.read_bytes() == png
    assert [(path.stat().st_ino, path.stat().st_mtime_ns) for path in (image, thumbnail)] == original_stats
    assert preferences.read_text() == "{\"custom\":true}"


def test_stored_placeholder_can_be_replaced_by_verified_metadata(
    options: Options, stored_game: tuple[dict, Path, Path], lookup_rule: str, response_mock: Callable[..., Any],
) -> None:
    entry, image, thumbnail = stored_game
    catalogue = options.dst / "catalogue.json"
    catalogue.write_bytes(encode([{**entry, "title": "ZZZ"}]))
    pipeline = build_provider(HttpClient(options.cache), environ={}, source_names=["hasheous"])

    with response_mock(lookup_rule):
        result = run(replace(options, src=None), pipeline)

    assert result.total == 1
    assert decode(catalogue.read_bytes())[0]["title"] == "Synthetic Game"
    assert image.exists() and thumbnail.exists()


@pytest.mark.parametrize("missing", ["image", "thumbnail", "both"])
def test_legacy_placeholder_with_missing_asset_never_resumes_automatic_cleanup(
    options: Options, provider: Any, stored_game: tuple[dict, Path, Path], missing: str,
) -> None:
    entry, image, thumbnail = stored_game
    catalogue = options.dst / "catalogue.json"
    original = encode([{**entry, "title": "ZZZ"}])
    catalogue.write_bytes(original)

    if missing in {"image", "both"}:
        image.unlink()

    if missing in {"thumbnail", "both"}:
        thumbnail.unlink()

    if missing == "thumbnail":
        result = run(replace(options, src=None), provider)
        assert result.total == 1
        assert image.exists()
        assert decode(catalogue.read_bytes())[0]["title"] == "Game"

    else:

        with pytest.raises(CatalogueError, match="Indexed image is missing"):
            run(replace(options, src=None), provider)

        assert catalogue.read_bytes() == original
        assert thumbnail.exists() == (missing == "image")


def test_cached_placeholder_response_is_ignored_offline_without_refresh(
    options: Options, nes: bytes, placeholder_rule: str, response_mock: Callable[..., Any],
) -> None:
    (options.src / "Game.nes").write_bytes(nes)
    pipeline = build_provider(HttpClient(options.cache), environ={}, source_names=["hasheous"])

    with response_mock(placeholder_rule):
        run(options, pipeline)

    catalogue = options.dst / "catalogue.json"
    original = catalogue.read_bytes()
    pipeline = build_provider(HttpClient(options.cache, offline=True), environ={}, source_names=["hasheous"])

    with response_mock([]) as mock:
        result = run(replace(options, src=None), pipeline)
        assert not mock.calls

    assert result.total == 1 and result.excluded == 0
    assert catalogue.read_bytes() == original


@pytest.mark.parametrize("failure", [OSError("Synthetic publication failure"), KeyboardInterrupt()])
def test_placeholder_move_keeps_source_when_publication_fails(
    options: Options, nes: bytes, placeholder_rule: str, response_mock: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch, failure: BaseException,
) -> None:
    source = options.src / "Game.nes"
    source.write_bytes(nes)
    pipeline = build_provider(HttpClient(options.cache), environ={}, source_names=["hasheous"])

    def fail(*args: Any, **kwargs: Any) -> None:
        raise failure

    monkeypatch.setattr("icat.operations.publication.publish_catalogue", fail)

    with response_mock(placeholder_rule), pytest.raises(type(failure), match=f"{failure}" or None):
        run(replace(options, move_roms=True), pipeline)

    assert source.read_bytes() == nes


@pytest.mark.parametrize("broken", ["image", "thumbnail", "parent", "different_bytes"])
def test_stored_placeholder_does_not_bypass_symlink_or_identity_guards(
    options: Options, provider: Any, stored_game: tuple[dict, Path, Path], tmp_path: Path, broken: str,
) -> None:
    entry, image, thumbnail = stored_game
    catalogue = options.dst / "catalogue.json"
    original = encode([{**entry, "title": "ZZZ"}])
    catalogue.write_bytes(original)
    thumb_bytes = thumbnail.read_bytes()

    if broken == "different_bytes":
        image.write_bytes(b"not the original ROM")

    else:
        target = {"image": image, "thumbnail": thumbnail, "parent": image.parent}[broken]
        outside = tmp_path / "outside"
        target.rename(outside)
        target.symlink_to(outside)

    with pytest.raises(CatalogueError, match="Symlink|identity/platform mismatch|Invalid iNES"):
        run(replace(options, src=None), provider)

    assert catalogue.read_bytes() == original
    assert thumbnail.read_bytes() == thumb_bytes
    assert image.exists()


def test_zzz_filename_alone_does_not_exclude_new_or_existing_rom(options: Options, provider: Any, nes: bytes) -> None:
    (options.src / "ZZZ.nes").write_bytes(nes)
    first = run(replace(options, move_roms=True), provider)

    second = run(replace(options, src=None), provider)

    assert first.total == second.total == 1
    assert first.excluded == second.excluded == 0
    assert decode((options.dst / "catalogue.json").read_bytes())[0]["title"] == "ZZZ"
