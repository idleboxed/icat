import json

import pytest

from icat.errors import CatalogueError
from icat.catalogue.codec import decode, create_empty_entry, encode, is_valid_image_path


@pytest.mark.parametrize(
    "name",
    [
        "../x.nes",
        "/x.nes",
        "NES//x.nes",
        "NES/./x.nes",
        "x\\a.nes",
        "C:/a.nes",
        "x/../a.nes",
        "x/",
        "CON.nes",
        "x/PRN",
        "x/a.nes.",
        "x/a.nes ",
        "x/\0",
    ],
)
def test_unsafe_paths_rejected(name: str) -> None:
    assert not is_valid_image_path(name)

    with pytest.raises(CatalogueError, match="filename"):
        encode([create_empty_entry(name, "a" * 64, "NES")])


def test_roundtrip_without_schema_version() -> None:
    entry = create_empty_entry("NES/ab/Game.nes", "a" * 64, "NES")

    encoded = encode([entry])

    assert decode(encoded) == [entry]
    assert "version" not in json.loads(encoded)


def test_unknown_keys_are_ignored_and_required_games_is_reported() -> None:
    entry = create_empty_entry("game.nes", "a" * 64, "NES")
    entry["future"] = {"value": 1}

    assert decode(json.dumps({"version": 999, "future": True, "games": [entry]}).encode()) == [
        {key: value for key, value in entry.items() if key != "future"}
    ]

    with pytest.raises(CatalogueError, match="games array"):
        decode(json.dumps({"future": True}).encode())


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("players", True),
        ("year", 0),
        ("region", "Japan"),
        ("region", ["World"]),
        ("description", "x" * 16385),
        ("title", "\x1b"),
        ("genre", ""),
        ("platform", "Unknown"),
    ],
)
def test_invalid_fields_rejected(key: str, value: object) -> None:
    entry = create_empty_entry("game.nes", "a" * 64, "NES")
    entry[key] = value

    with pytest.raises(CatalogueError, match="Invalid"):
        encode([entry])


def test_case_insensitive_collisions_and_duplicate_hash_rejected() -> None:
    first = create_empty_entry("NES/aa/game.nes", "a" * 64, "NES")
    second = create_empty_entry("nes/AA/GAME.NES", "b" * 64, "NES")

    with pytest.raises(CatalogueError, match="duplicate image"):
        encode([first, second])

    second["image"] = "other.nes"
    second["hash"] = first["hash"]

    with pytest.raises(CatalogueError, match="duplicate SHA"):
        encode([first, second])
