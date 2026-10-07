"""Synthetic multi-variant ZIP/7z selection and verified whole-archive removal."""

import hashlib
import json
import re
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from icat.roms.archives.selection import choose_rom
from icat.errors import SourceError
from icat.catalogue.codec import decode
from icat.reporting.diagnostics import Diagnostics
from icat.roms.staging import stage_source
from icat.operations.options import Options
from icat.operations.session import run


@pytest.fixture(params=["zip", "7z"])
def archive_factory(
    request: pytest.FixtureRequest, sevenzip_archive: Callable[..., Path],
) -> Callable[[Path, Sequence[tuple[str, bytes]]], Path]:
    def create(root: Path, entries: Sequence[tuple[str, bytes]]) -> Path:
        stem = re.sub(r"\([^()]*\)|\[[^][]*\]", " ", Path(entries[0][0]).stem)
        stem = " ".join(stem.split())
        path = root / f"{stem}.{request.param}"

        if request.param == "7z":
            return sevenzip_archive(path, entries)

        with zipfile.ZipFile(path, "w") as archive:

            for name, data in entries:
                archive.writestr(name, data)

        return path

    return create


@pytest.mark.parametrize(("names", "expected"), [
    (["Game (U).nes", "Game.nes", "Game (E) [!].nes"], "Game.nes"),
    (["Game (Japan) [!].nes", "Game (USA).nes"], "Game (Japan) [!].nes"),
    (["Game (U).nes", "Game (E).nes"], "Game (E).nes"),
    (["one/Game (U).NES", "two/Game (E).nes"], "two/Game (E).nes"),
    (["Double Dribble (E) [!].nes", "Double Dribble (PC10).nes"], "Double Dribble (E) [!].nes"),
    (["TB NFL 2007 Playoffs (Hack).nes", "Tecmo Bowl (U) (PRG0) [!].nes", "Tecmo Bowl (PC10).nes"],
     "Tecmo Bowl (U) (PRG0) [!].nes"),
    (["Volleyball (J) [FDS].fds", "Volleyball (PC10).nes", "Volleyball (UE) [!].nes"], "Volleyball (UE) [!].nes"),
])
def test_selects_group_then_exact_name_verified_marker_length_and_alphabet(
    archive_factory: Callable[..., Path], tmp_path: Path, nes: bytes, names: list[str], expected: str,
) -> None:
    entries = [(name, nes[:-1] + bytes([number])) for number, name in enumerate(names)]
    chosen = dict(entries)[expected]
    path = archive_factory(tmp_path, entries)
    archive_title = re.sub(r"\([^()]*\)|\[[^][]*\]", " ", Path(expected).stem)
    archive_title = " ".join(archive_title.split())
    matching_path = path.with_name(f"{archive_title}{path.suffix}")

    if matching_path != path:
        path.rename(matching_path)
        path = matching_path

    original = path.read_bytes()

    result = stage_source(path, tmp_path)

    assert result.roms[0].entry["image"] == expected.split("/")[-1]
    assert result.roms[0].path.read_bytes() == chosen
    assert result.roms[0].entry["hash"] == hashlib.sha256(chosen).hexdigest()
    assert path.read_bytes() == original


@pytest.mark.parametrize("reverse", [False, True])
def test_archive_title_selects_only_its_variant_family_independent_of_order(reverse: bool) -> None:
    diagnostics = Diagnostics()
    names = [
        "Super Mary [p1][!].nes",
        "Super Mario Bros. (USA).nes",
        "Super Mario Bros. (Europe) [!].nes",
        "Other Game [!].nes",
    ]
    names = list(reversed(names)) if reverse else names

    selected = choose_rom(names, Path("Super Mario Bros..7z"), diagnostics)

    assert selected == "Super Mario Bros. (Europe) [!].nes"
    event = diagnostics.get_snapshot()["events"][0]
    assert event["prefix"] == "super mario bros"
    assert event["group_size"] == 2
    assert event["group_count"] == 3


@pytest.mark.parametrize("archive", ["Public Domain.7z", "Nintendo Demos SDK.zip", "A&S NES Hacks.7z"])
def test_collection_without_an_exact_title_family_is_kept(archive: str) -> None:
    diagnostics = Diagnostics()
    names = ["First Game (USA) [!].nes", "Second Game (Japan) [!].nes", "Third Demo.nes"]

    assert choose_rom(names, Path(archive), diagnostics) is None

    event = diagnostics.get_snapshot()["events"][0]
    assert event["outcome"] == "multiple_roms"
    assert event["reason"] == "archive_name_mismatch"
    assert event["group_count"] == 3


def test_archive_matching_group_and_verified_selection_are_journaled(
    archive_factory: Callable[..., Path], tmp_path: Path, nes: bytes,
) -> None:
    diagnostics = Diagnostics()
    path = archive_factory(tmp_path, [
        ("Game (PC10).nes", nes), ("Game (Japan) [!].nes", nes), ("Other.nes", nes),
    ])

    source = stage_source(path, tmp_path, diagnostics=diagnostics)

    assert source.roms[0].entry["image"] == "Game (Japan) [!].nes"
    event = diagnostics.get_snapshot()["events"][0]
    assert event["group_count"] == 2
    assert event["group_size"] == 2
    assert event["count"] == 3
    assert event["rule"] == "verified_then_shortest_then_alphabetical"


def test_selected_pc10_like_tail_is_kept_without_changing_archive_or_choosing_another_rom(
    archive_factory: Callable[..., Path], tmp_path: Path, nes: bytes,
) -> None:
    # Synthetic reproduction of the observed shape, not a real PlayChoice BIOS fixture.
    path = archive_factory(tmp_path, [("Game (PC10).nes", nes + b"B" * 8192), ("Game (USA) [b1].nes", nes)])
    original = path.read_bytes()

    source = stage_source(path, tmp_path)

    assert path.read_bytes() == original
    rom = source.roms[0]
    assert rom.entry["image"] == "Game (PC10).nes"
    assert rom.path.read_bytes() == nes + b"B" * 8192
    assert rom.lookups[0] == (hashlib.sha1(rom.path.read_bytes()).hexdigest(), len(nes) + 8192)


def test_invalid_selected_rom_does_not_fall_back_to_a_longer_name(
    archive_factory: Callable[..., Path], tmp_path: Path, nes: bytes,
) -> None:
    path = archive_factory(tmp_path, [("Game.nes", b"invalid"), ("Game (U).nes", nes)])
    original = path.read_bytes()

    with pytest.raises(SourceError, match="Invalid iNES header"):
        stage_source(path, tmp_path)

    assert path.read_bytes() == original


def test_invalid_verified_rom_does_not_fall_back_to_an_unmarked_variant(
    archive_factory: Callable[..., Path], tmp_path: Path, nes: bytes,
) -> None:
    path = archive_factory(tmp_path, [("Game (Japan) [!].nes", b"invalid"), ("Game (U).nes", nes)])
    original = path.read_bytes()

    with pytest.raises(SourceError, match="Invalid iNES header"):
        stage_source(path, tmp_path)

    assert path.read_bytes() == original


def test_move_removes_whole_archive_only_after_selected_rom_is_published(
    archive_factory: Callable[..., Path], options: Options, provider: Any, nes: bytes,
) -> None:
    path = archive_factory(options.src, [("Game (U).nes", nes[:-1] + b"D"), ("Game (E).nes", nes)])
    archive_digest = hashlib.sha256(path.read_bytes()).hexdigest()

    result = run(replace(options, move_roms=True), provider)

    assert result.total == result.imported == result.removed == 1
    assert not path.exists()
    entry = decode((options.dst / "catalogue.json").read_bytes())[0]
    assert (options.dst / "images" / entry["image"]).read_bytes() == nes
    journal = json.loads(next(options.logs.rglob("journal.json")).read_text())
    assert journal["sources"][0]["sha256"] == archive_digest
    selection = next(event for event in journal["diagnostics"]["events"] if event["outcome"] == "archive_selection")
    assert selection["selected"] == "Game (E).nes"
    assert selection["count"] == 2
    assert selection["rule"] == "shortest_then_alphabetical"


def test_selected_archive_stays_intact_when_publication_fails(
    archive_factory: Callable[..., Path], options: Options, provider: Any, nes: bytes, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = archive_factory(options.src, [("Game.nes", nes), ("Game (U).nes", nes[:-1] + b"D")])
    original = path.read_bytes()

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("Synthetic publication failure")

    monkeypatch.setattr("icat.operations.publication.publish_catalogue", fail)

    with pytest.raises(OSError, match="Synthetic publication failure"):
        run(replace(options, move_roms=True), provider)

    assert path.read_bytes() == original


def test_solid_variant_selection_keeps_only_selected_rom(
    tmp_path: Path, nes: bytes, sevenzip_archive: Callable[..., Path],
) -> None:
    path = sevenzip_archive(tmp_path / "Game.7z", [("Game.nes", nes), ("Game (U).nes", nes)])

    result = stage_source(path, tmp_path)

    assert result.roms[0].entry["image"] == "Game.nes"
    assert result.roms[0].path.read_bytes() == nes
    assert path.is_file()


def test_zip_duplicate_names_are_rejected_before_selection(tmp_path: Path, nes: bytes) -> None:
    path = tmp_path / "duplicate.zip"

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Game.nes", nes)

        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("Game.nes", nes)

    with pytest.raises(SourceError, match="Duplicate ZIP member"):
        stage_source(path, tmp_path)

    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("archive_name", ["Unrelated", "Short", "!!!"])
def test_single_title_group_does_not_require_matching_archive_name(
    archive_factory: Callable[..., Path], tmp_path: Path, nes: bytes, archive_name: str,
) -> None:
    path = archive_factory(tmp_path, [
        ("Long Game Title (U) [!].nes", nes[:-1] + b"D"),
        ("Long Game Title (E) [!].nes", nes),
    ])
    renamed = path.with_name(f"{archive_name}{path.suffix}")
    path.rename(renamed)
    diagnostics = Diagnostics()

    result = stage_source(renamed, tmp_path, diagnostics=diagnostics)

    assert result.roms[0].entry["image"] == "Long Game Title (E) [!].nes"
    assert result.roms[0].path.read_bytes() == nes
    event = diagnostics.get_snapshot()["events"][0]
    assert event["prefix"] == "long game title"
    assert event["group_count"] == 1
    assert event["group_size"] == 2


@pytest.mark.parametrize("names", [
    ["Game (U).nes", "!!!.nes"],
    ["Game (U).nes", "Game (E).nes", "(Japan) [!].nes"],
])
def test_unnamed_candidate_cannot_be_ignored_to_claim_one_group(names: list[str]) -> None:
    diagnostics = Diagnostics()

    assert choose_rom(names, Path("Unrelated.zip"), diagnostics) is None

    assert diagnostics.get_snapshot()["events"][0]["reason"] == "unclassified_title"


@pytest.mark.parametrize("same_title", [False, True])
def test_move_with_unrelated_archive_name_imports_only_a_single_group(
    archive_factory: Callable[..., Path], options: Options, provider: Any, nes: bytes, same_title: bool,
) -> None:
    second = "Game (E).nes" if same_title else "Other (E).nes"
    path = archive_factory(options.src, [("Game (U).nes", nes[:-1] + b"D"), (second, nes)])
    renamed = path.with_name(f"Unrelated{path.suffix}")
    path.rename(renamed)
    path = renamed
    original = path.read_bytes()

    result = run(replace(options, move_roms=True), provider)

    assert result.imported == result.removed == int(same_title)

    if same_title:
        assert not path.exists()
        entry = decode((options.dst / "catalogue.json").read_bytes())[0]
        assert (options.dst / "images" / entry["image"]).read_bytes() == nes

    else:
        assert path.read_bytes() == original


def test_unrelated_archive_name_does_not_bypass_invalid_rom_check(
    archive_factory: Callable[..., Path], tmp_path: Path, nes: bytes,
) -> None:
    path = archive_factory(tmp_path, [("Game (E) [!].nes", b"invalid"), ("Game (U).nes", nes)])
    renamed = path.with_name(f"Unrelated{path.suffix}")
    path.rename(renamed)
    original = renamed.read_bytes()

    with pytest.raises(SourceError, match="Invalid iNES header"):
        stage_source(renamed, tmp_path)

    assert renamed.read_bytes() == original
