"""Bounded clrmamepro DAT reader; preserve repeated fields instead of silently overwriting."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field


TOKEN = re.compile(r"\s+|\#[^\n]*|\"(?:\\.|[^\"\\])*\"|[()]|[^\s()\"\x00-\x1f]+")
LIMIT = 16 * 1024 * 1024


@dataclass
class DatNode:
    fields: dict[str, list[str | DatNode]] = field(default_factory=dict)

    def get_scalar(self, name: str) -> str | None:
        values = self.fields.get(name, [])

        if not values:
            return None

        if len(values) != 1 or not isinstance(values[0], str):
            raise ValueError(f"Expected one DAT scalar: {name}")

        return values[0]

    def get_children(self, name: str) -> list[DatNode]:
        values = self.fields.get(name, [])

        if any(not isinstance(value, DatNode) for value in values):
            raise ValueError(f"Expected DAT blocks: {name}")

        return values


def read_dat(data: bytes) -> Iterator[tuple[str, DatNode]]:
    """Yield top-level blocks, rejecting malformed/truncated input and excessive nesting."""

    if len(data) > LIMIT:
        raise ValueError("DAT exceeds size limit")

    text = data.decode("utf-8-sig")

    def iter_tokens() -> Iterator[str]:
        position = 0

        for match in TOKEN.finditer(text):

            if match.start() != position:
                raise ValueError("Invalid DAT token")

            position = match.end()
            token = match.group()

            if not token.isspace() and not token.startswith("#"):
                yield token

        if position != len(text):
            raise ValueError("Incomplete DAT token")

    stream = iter(iter_tokens())

    def read_required_token() -> str:
        token = next(stream, None)

        if token is None:
            raise ValueError("Truncated DAT block")

        return token

    def read_block(depth: int) -> DatNode:

        if depth > 8:
            raise ValueError("DAT nesting exceeds limit")

        node = DatNode()

        while (key := read_required_token()) != ")":

            if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9-]*", key):
                raise ValueError("Invalid DAT field name")

            token = read_required_token()

            if token == ")":
                raise ValueError("Missing DAT field value")

            if token == "(":
                value = read_block(depth + 1)

            elif token.startswith("\""):
                value = re.sub(r"\\([\"\\])", r"\1", token[1:-1])

            else:
                value = token

            node.fields.setdefault(key, []).append(value)

        return node

    for tag in stream:

        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9-]*", tag) or read_required_token() != "(":
            raise ValueError("Expected DAT block")

        yield tag, read_block(1)
