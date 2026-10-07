


from collections.abc import Callable
from typing import Any

import pytest

from icat.metadata.provider import Provider
from icat.roms.types import Rom
from icat.metadata.sources.openvgdb import URL, OpenVgdbSource


def test_openvgdb_identity_html_normalization_and_source_tracking(
    source: OpenVgdbSource, rom: Rom, openvgdb_archive: Callable[..., bytes], response_mock: Callable[..., Any],
) -> None:

    with response_mock(f"GET {URL} -> 200 :".encode() + openvgdb_archive()):
        result = Provider([source]).lookup(rom)

    assert result.fields == {
        "title": "Synthetic Game",
        "description": "Explore & rescue.\n\nSecond paragraph.",
        "genre": "Platformer",
        "year": 1991,
        "region": ["Japan"],
    }
    assert result.sources == [URL]
    assert set(result.field_sources.values()) == {"openvgdb"}

@pytest.mark.parametrize("change", [{"digest": "0" * 40}, {"size": 1}, {"platform": 33}])
def test_openvgdb_never_matches_only_a_name(
    source: OpenVgdbSource, rom: Rom, openvgdb_archive: Callable[..., bytes],
    response_mock: Callable[..., Any], change: dict,
) -> None:

    with response_mock(f"GET {URL} -> 200 :".encode() + openvgdb_archive(**change)):
        assert Provider([source]).lookup(rom).fields == {}

def test_openvgdb_rejects_conflicting_descriptions_and_unrelated_release_dates(
    source: OpenVgdbSource, rom: Rom, openvgdb_archive: Callable[..., bytes], response_mock: Callable[..., Any],
) -> None:
    releases = [
        (21, "Game", "First description", "Action", "1992"),
        (21, "Game", "Different description", "Action", "1993"),
    ]

    with response_mock(f"GET {URL} -> 200 :".encode() + openvgdb_archive(releases=releases)):
        result = Provider([source]).lookup(rom)

    assert "description" not in result.fields and "year" not in result.fields
    assert result.fields["genre"] == "Action"

@pytest.mark.parametrize(
    ("categories", "expected"),
    [
        ("Strategy,Turn-Based,Sci-Fi", "Strategy"),
        ("Action,Shooter,Scrolling", "Shooter"),
        ("Sports,Traditional,Baseball,Sim", "Sports"),
        ("Driving,Racing,Arcade", "Racing"),
        ("Action Adventure,Modern", "Action Adventure"),
        ("Role-Playing,Console-style RPG,First-Person", "RPG"),
        ("Action,Platformer,2D", "Platformer"),
        ("Sports,Other", "Sports"),
        ("Other,Sports", "Sports"),
        ("Racing,Miscellaneous,Sim", "Racing"),
        ("Action,Platformer,Other", "Platformer"),
        ("Other,Arcade", "Arcade"),
        ("Arcade,Other", "Arcade"),
        ("Other,Miscellaneous", "Other"),
        ("Horror,Board", "Board Games"),
        ("Life Simulation,Horror", "Simulation"),
        ("Education,2D", "Edutainment"),
    ],
)
def test_openvgdb_does_not_use_camera_or_setting_as_genre(
    source: OpenVgdbSource, rom: Rom, openvgdb_archive: Callable[..., bytes],
    response_mock: Callable[..., Any], categories: str, expected: str,
) -> None:
    data = openvgdb_archive(releases=[(13, "Game", "A description.", categories, "1991")])

    with response_mock(f"GET {URL} -> 200 :".encode() + data):
        result = Provider([source]).lookup(rom)

    assert result.fields["genre"] == expected

@pytest.mark.parametrize(("genres", "expected"), [
    (["Other", "Sports"], "Sports"), (["Sim", "Sports"], "Sports"),
    (["Other", "Sports", "Racing"], None),
])
def test_openvgdb_release_consensus_ignores_other_but_not_specific_conflicts(
    source: OpenVgdbSource, rom: Rom, openvgdb_archive: Callable[..., bytes],
    response_mock: Callable[..., Any], genres: list[str], expected: str | None,
) -> None:
    releases = [(13, "Game", "Description.", genre, "1991") for genre in genres]

    with response_mock(f"GET {URL} -> 200 :".encode() + openvgdb_archive(releases=releases)):
        result = Provider([source]).lookup(rom)

    assert result.fields.get("genre") == expected

def test_verified_openvgdb_placeholder_card_is_ignored(
    source: OpenVgdbSource, rom: Rom, openvgdb_archive: Callable[..., bytes], response_mock: Callable[..., Any],
) -> None:
    releases = [(13, "ZZZ", "Non-game cartridge.", "Other", "1991")]

    with response_mock(f"GET {URL} -> 200 :".encode() + openvgdb_archive(releases=releases)):
        result = Provider([source]).lookup(rom)

    assert result.identifiers == {}
    assert result.sources == []
    assert result.fields == {}
