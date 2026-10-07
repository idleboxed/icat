"""Canonical genre vocabulary and explicit aliases, without fuzzy matching.

SPDX-License-Identifier: BSD-3-Clause
"""

import logging
import re
from collections.abc import Iterable
from pathlib import Path

from ..errors import CatalogueError
from ..catalogue.codec import is_clean_text
from ..reporting.diagnostics import Diagnostics
from ..io import paths


logger = logging.getLogger(__name__)

GENRE_ALIASES = {
    "RPG": ("Role-Playing", "Role-playing (RPG)", "Console-style RPG"),
    "Simulation": (
        "Simulator", "Breeding/Constructing", "Life Simulation", "Construction and Management Simulation",
    ),
    "Strategy": ("Tactical", "Real Time Strategy (RTS)", "Turn-based strategy (TBS)"),
    "Racing": ("Driving", "Formula One", "Vehicle Simulation"),
    "Sports": (
        "Sport", "Baseball", "Basketball", "Bowling", "Boxing", "Fishing", "Football", "Golf", "Hockey",
        "Horse Racing", "Rugby", "Soccer", "Tennis", "Volleyball", "Wrestling",
    ),
    "Board Games": ("Card & Board Game", "Card Game", "Chess", "Mahjong", "Board"),
    "Quiz": ("Quiz/Trivia", "Trivia / Game Show"),
    "Shooter": ("Light Gun",),
    "Adventure": ("Point-and-click",),
    "Other": ("Miscellaneous",),
    "Action": (),
    "Action Adventure": (),
    "Arcade": (),
    "Beat 'em up": (),
    "Fighting": (),
    "Platformer": ("Platform",),
    "Puzzle": (),
    "Flight": ("Flight Simulator",),
    "Gambling": (),
    "Music": (),
    "Pinball": (),
    "Edutainment": ("Education",),
    "Visual Novel": (),
}
GENRES = frozenset(GENRE_ALIASES)


def normalize_genre_key(value: str) -> str:
    value = re.sub(r"[-_\u2010-\u2015]", " ", value).casefold()
    value = re.sub(r"\s*/\s*", "/", value)
    return " ".join(value.split())


ALIASES = {
    normalize_genre_key(alias): genre
    for genre, aliases in GENRE_ALIASES.items() for alias in (genre, *aliases)
}


def normalize_genre(value: object) -> str | None:
    """Return a canonical genre only for a listed name or spelling variant."""
    return ALIASES.get(normalize_genre_key(value)) if isinstance(value, str) else None


def select_genre(values: Iterable[object]) -> str | None:
    """Prefer a known concrete genre; retain input priority among equally specific names."""
    choices = [value for value in values if isinstance(value, str) and value.strip()]
    known = [genre for value in choices if (genre := normalize_genre(value)) is not None]
    fallback = known[0] if known else choices[0] if choices else None
    return next((genre for genre in known if genre != "Other"), fallback)


def resolve_genre(value: object, image: str, diagnostics: Diagnostics, *, existing: bool = False) -> str | None:
    """Normalize recognized genres; reject unknown values as missing, never as Other."""

    if value is None:
        return None

    if not is_clean_text(value) or not value.strip():
        raise CatalogueError("Invalid genre", path=Path(image).name)

    genre = normalize_genre(value)

    if genre is not None:
        return genre

    logger.warning("%s — unknown genre %r; using null", paths.quote_path(Path(image).name), value)
    diagnostics.emit("issue", "unknown_genre", genre=value, image=image, existing=existing)
    return None
