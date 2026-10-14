"""Shared rules for missing and placeholder metadata."""

import re
from pathlib import Path


def is_description_missing(entry: dict) -> bool:
    """Report whether an entry has no meaningful description."""
    value = entry.get("description")

    if not isinstance(value, str) or not value.strip():
        return True

    normalized = re.sub(r"\([^)]*\)|[^\w]", "", value).casefold()
    names = (entry.get("title", ""), Path(entry["image"]).stem)
    return normalized in {re.sub(r"\([^)]*\)|[^\w]", "", name).casefold() for name in names}

def is_placeholder_title(value: object) -> bool:
    """Report whether a title is the provider's known placeholder value."""
    return isinstance(value, str) and value.strip().casefold() == "zzz"
