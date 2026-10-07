"""Deterministic name-based selection shared by ZIP and 7z importers.

SPDX-License-Identifier: BSD-3-Clause
"""

import logging
import re
from pathlib import Path, PurePosixPath

from ...reporting.diagnostics import Diagnostics


logger = logging.getLogger(__name__)


def _extract_title_tokens(value: str) -> tuple[str, ...]:
    """Return a conservative title identity without dump annotations.

    Archive sets conventionally put regions, revisions and dump flags in
    parentheses or brackets. Treating the remaining complete title as the
    family identity is deliberately stricter than guessing from a dominant
    first word: an ambiguous collection must stay untouched.
    """
    value = re.sub(r"\([^()]*\)|\[[^][]*\]", " ", value)
    return tuple(part.casefold() for part in re.findall(r"[^\W_]+", value))


def choose_rom(names: list[str], path: Path, diagnostics: Diagnostics) -> str | None:
    """Preserve archive-title matches, falling back only to a single complete family."""

    if not names:
        diagnostics.emit("source", "no_supported_roms", path=f"{path}")
        logger.warning("%s — no supported ROMs; kept", path)
        return None

    if len(names) == 1:
        return names[0]

    if any(PurePosixPath(name).suffix.lower() == ".chd" for name in names):
        diagnostics.emit("source", "multiple_chd_candidates", path=f"{path}", count=len(names))
        logger.warning("%s — CHD archives must contain only one candidate image; kept", path)
        return None

    stems = {name: PurePosixPath(name).stem for name in names}
    groups: dict[tuple[str, ...], list[str]] = {}

    for name, stem in stems.items():

        if words := _extract_title_tokens(stem):
            groups.setdefault(words, []).append(name)

    if not groups:
        diagnostics.emit("source", "multiple_roms", path=f"{path}", count=len(names), reason="no_alphanumeric_prefix")
        logger.warning("%s — multiple ROMs without a name prefix; kept", path)
        return None

    archive_title = _extract_title_tokens(path.stem)
    title = archive_title
    members = groups.get(archive_title)

    if members is None:

        if len(groups) != 1:
            diagnostics.emit(
                "source", "multiple_roms", path=f"{path}", count=len(names), reason="archive_name_mismatch",
                archive_title=" ".join(archive_title), group_count=len(groups),
            )
            logger.warning(
                "%s — %d distinct ROM titles do not unambiguously match the archive name; kept",
                path, len(groups),
            )
            return None

        title, members = next(iter(groups.items()))

        if len(members) != len(names):
            diagnostics.emit(
                "source", "multiple_roms", path=f"{path}", count=len(names), reason="unclassified_title",
                group_count=len(groups),
            )
            logger.warning("%s — multiple ROMs include an unclassified title; kept", path)
            return None

    prefix = " ".join(title)
    exact = {name for name in members if stems[name].casefold() == path.stem.casefold()}

    def get_member_sort_key(name: str) -> tuple[bool, bool, int, str, str, str]:
        return (
            name not in exact, "[!]" not in stems[name],
            len(stems[name]), stems[name].casefold(), stems[name], name,
        )

    selected = min(members, key=get_member_sort_key)
    rule = (
        "exact_title" if selected in exact else
        "verified_then_shortest_then_alphabetical" if "[!]" in stems[selected] else
        "shortest_then_alphabetical"
    )
    diagnostics.emit(
        "source", "archive_selection", path=f"{path}", count=len(names), selected=selected, prefix=prefix, rule=rule,
        group_count=len(groups), group_size=len(members),
    )
    logger.warning(
        "%s — selected %s; %d ROMs, %d matching title variants; other variants not imported",
        path, Path(PurePosixPath(selected).name), len(names), len(members),
    )
    return selected
