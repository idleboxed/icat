"""The IGUI catalogue contract (no ROM or preview I/O)."""

import json
import re
from pathlib import Path

from ..errors import CatalogueError
from ..io import paths


LIMIT = 16 * 1024 * 1024
FIELDS = {"hash", "platform", "region", "image", "title", "year", "players", "genre", "description"}


def is_clean_text(value: object, *, multiline: bool = False) -> bool:
    return isinstance(value, str) and not any(
        (ord(character) < 32 or 127 <= ord(character) <= 159)
        and not (multiline and character in "\r\n\t") for character in value
    )


def is_valid_filename(name: object) -> bool:

    if not isinstance(name, str) or not is_clean_text(name):
        return False

    reserved = {
        "CON", "PRN", "AUX", "NUL",
        *(f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)),
    }
    return (
        0 < len(name.encode("utf-8")) <= 255
        and name not in (".", "..")
        and not name.endswith((".", " "))
        and not any(character in name for character in "/\\:<>\"|?*")
        and name.split(".")[0].upper() not in reserved
    )


def is_valid_image_path(name: object) -> bool:
    return (
        isinstance(name, str)
        and len(name.encode("utf-8")) <= 512
        and all(is_valid_filename(part) for part in name.split("/"))
    )


def validate(games: list[dict]) -> None:
    hashes, names = set(), set()

    for entry in games:

        if not isinstance(entry, dict):
            raise CatalogueError("Invalid catalogue entry", path=paths.CATALOGUE_FILE)

        digest, name = entry.get("hash"), entry.get("image")

        if not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest) or digest in hashes:
            raise CatalogueError("Invalid or duplicate SHA-256", path=paths.CATALOGUE_FILE)

        if not is_valid_image_path(name) or name.casefold() in names:
            raise CatalogueError(
                "Invalid or duplicate image filename",
                path=paths.get_image_path(name) if isinstance(name, str) else paths.CATALOGUE_FILE,
            )

        image = paths.get_image_path(name)
        hashes.add(digest)
        names.add(name.casefold())
        title, platform = entry.get("title"), entry.get("platform")

        if not is_clean_text(title) or not title.strip() or not isinstance(platform, str):
            raise CatalogueError("Invalid title/platform", path=image)

        if not re.fullmatch("[A-Z0-9]{1,6}", platform):
            raise CatalogueError("Invalid platform", path=image)

        for field, maximum in (("year", 9999), ("players", 255)):
            value = entry.get(field)

            if value is not None and (type(value) is not int or not 1 <= value <= maximum):
                raise CatalogueError(f"Invalid {field}", path=image)

        regions = entry.get("region")

        if regions is not None:

            if not isinstance(regions, list) or any(
                not is_clean_text(region) or not region.strip()
                or region.strip().lower() in ("?", "-", "world") or "," in region
                for region in regions
            ):
                raise CatalogueError("Invalid region", path=image)

            if sum(len(region.encode("utf-8")) for region in regions) > 128:
                raise CatalogueError("Region too long", path=image)

        genre, description = entry.get("genre"), entry.get("description")

        if genre is not None and (not is_clean_text(genre) or not genre.strip()):
            raise CatalogueError("Invalid genre", path=image)

        if description is not None and (
            not is_clean_text(description, multiline=True) or len(description.encode("utf-8")) > 16384
        ):
            raise CatalogueError("Invalid description", path=image)


def filter_known_fields(games: list[dict]) -> list[dict]:
    return [
        {key: value for key, value in entry.items() if key in FIELDS} if isinstance(entry, dict) else entry
        for entry in games
    ]


def encode(games: list[dict]) -> bytes:
    games = filter_known_fields(games)
    validate(games)
    data = (json.dumps({"games": games}, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    if len(data) > LIMIT:
        raise CatalogueError("Catalogue exceeds IGUI's 16 MiB limit", path=paths.CATALOGUE_FILE)

    return data


def decode(data: bytes) -> list[dict]:
    try:
        doc = json.loads(data)

    except (ValueError, UnicodeError) as exc:
        raise CatalogueError("Invalid catalogue JSON", path=paths.CATALOGUE_FILE) from exc

    if len(data) > LIMIT or not isinstance(doc, dict) or not isinstance(doc.get("games"), list):
        raise CatalogueError("Expected IGUI catalogue with a games array", path=paths.CATALOGUE_FILE)

    games = filter_known_fields(doc["games"])
    validate(games)
    return games


def create_empty_entry(name: str, digest: str, platform: str) -> dict:
    return dict(
        hash=digest,
        platform=platform,
        image=name,
        title=Path(name).stem,
        region=None,
        year=None,
        players=None,
        genre=None,
        description=None,
    )
