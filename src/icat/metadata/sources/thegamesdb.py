"""Optional, bounded TheGamesDB batches selected by verified game and platform IDs.

SPDX-License-Identifier: BSD-3-Clause
"""

import re
import threading
from collections.abc import Iterator, Sequence
from itertools import batched
from urllib.parse import urlencode

from ...databases.cache import DatasetCache
from ..genres import select_genre
from ...io.http import HttpClient, NetworkError
from ..types import LookupContext, Metadata
from .base import MetadataSource


BASE = "https://api.thegamesdb.net/v1"


def parse_identifier(value: object) -> str | None:

    if type(value) not in (str, int):
        return None

    text = f"{value}"
    return f"{int(text)}" if re.fullmatch(r"[0-9]{1,12}", text) and int(text) > 0 else None


class TheGamesDbSource(MetadataSource):
    name = "thegamesdb"
    provides = frozenset({"title", "description", "year", "players", "genre"})
    hosts = frozenset({"api.thegamesdb.net"})
    key_env = "ICAT_THEGAMESDB_API_KEY"
    batch_size = 20

    def __init__(self, http: HttpClient, datasets: DatasetCache, *, key: str | None = None) -> None:
        super().__init__(http, datasets, key=key)
        self.genres_lock = threading.Lock()
        self.genres_loaded = False
        self.genres: dict[str, str | None] | None = None

    def get_skip_reason(self, context: LookupContext) -> str | None:
        reason = super().get_skip_reason(context)

        if reason:
            return reason

        if any(
            parse_identifier(context.identifiers.get(f"TheGamesDb:{kind}")) is None for kind in ("game", "platform")
        ):
            return "missing_identifier"

        return None

    def iter_batch_indexes(
        self, contexts: Sequence[LookupContext], pending: Sequence[int],
    ) -> Iterator[tuple[int, ...]]:
        by_id: dict[str, list[int]] = {}

        for index in pending:
            game_id = parse_identifier(contexts[index].identifiers.get("TheGamesDb:game"))
            by_id.setdefault(game_id, []).append(index)

        # One request per distinct ID in this source pass. All ROM variants receive
        # that response but still undergo their own platform and field validation.

        for identities in batched(sorted(by_id, key=int), self.batch_size):
            yield tuple(index for game_id in identities for index in by_id[game_id])

    def fetch_genre_names(self) -> dict[str, str | None] | None:
        # All workers share one bounded dictionary request, including a failed attempt.

        with self.genres_lock:

            if self.genres_loaded:

                if self.genres is None:
                    self.http.diagnostics.emit("issue", "genre_dictionary_unavailable")

                return self.genres

            self.genres_loaded = True

            try:
                query = urlencode({"apikey": self.key})
                response = self.get_json(f"{BASE}/Genres?{query}")

                if response is None:
                    raise ValueError("Genre dictionary unavailable")

                names = self.require_mapping(response.get("data")).get("genres")

                if not isinstance(names, dict) or not names or len(names) > 1024:
                    raise ValueError("Invalid genre dictionary")

                parsed = {}

                for key, genre in names.items():
                    genre_id = parse_identifier(key)

                    if genre_id is None or not isinstance(genre, dict) or genre_id in parsed:
                        raise ValueError("Invalid genre dictionary identity")

                    if "id" in genre and parse_identifier(genre["id"]) != genre_id:
                        raise ValueError("Genre dictionary identity mismatch")

                    parsed[genre_id] = self.normalize_text(genre.get("name"))

                self.genres = parsed

            except (NetworkError, OSError, ValueError) as exc:
                self.report_error(exc)

            return self.genres

    def fetch(self, context: LookupContext) -> Metadata:
        return self.fetch_many([context])[0]

    def fetch_many(self, contexts: Sequence[LookupContext]) -> list[Metadata]:
        identities = [
            (parse_identifier(context.identifiers.get("TheGamesDb:game")),
             parse_identifier(context.identifiers.get("TheGamesDb:platform")))
            for context in contexts
        ]
        requested = {game_id for game_id, platform_id in identities if game_id and platform_id}

        if len(requested) > self.batch_size:
            raise ValueError("TheGamesDB batch exceeds 20 distinct game IDs")

        results = [Metadata(outcome="no_match" if game_id and platform_id else "missing_identifier")
                   for game_id, platform_id in identities]

        if not requested:
            return results

        query = urlencode({
            "apikey": self.key, "id": ",".join(sorted(requested, key=int)),
            "fields": "overview,players,genres,platform",
        })
        response = self.get_json(f"{BASE}/Games/ByGameID?{query}")

        if response is None:
            return results

        # We request at most one page. Never follow server URLs containing credentials.
        pages = response.get("pages", {})

        if not isinstance(pages, dict) or pages.get("next"):
            raise ValueError("Unexpected pagination in bounded game batch")

        games = self.require_mapping(response.get("data")).get("games")

        if not isinstance(games, list) or len(games) > self.batch_size:
            raise ValueError("Invalid game batch response")

        by_id = {}
        ambiguous = set()

        for game in games:

            if not isinstance(game, dict):
                raise ValueError("Invalid game batch entry")

            game_id = parse_identifier(game.get("id"))

            if game_id not in requested:
                self.http.diagnostics.emit("issue", "unexpected_game_identity")
                continue

            if game_id in by_id:
                ambiguous.add(game_id)

            by_id[game_id] = game

        for game_id in ambiguous:
            del by_id[game_id]
            self.http.diagnostics.emit("issue", "ambiguous_game_identity")

        for index, (context, (game_id, platform_id)) in enumerate(zip(contexts, identities, strict=True)):

            if not game_id or not platform_id:
                continue

            game = by_id.get(game_id)

            if game is None or parse_identifier(game.get("platform")) != platform_id:
                continue

            result = Metadata(
                fields={
                    "title": self.normalize_text(game.get("game_title")),
                    "description": self.normalize_text(game.get("overview"), html=True, multiline=True),
                    "year": self.parse_year(game.get("release_date")),
                    "players": game.get("players"),
                },
                sources=[f"https://thegamesdb.net/game.php?id={game_id}"],
            )
            genres = game.get("genres")

            if "genre" in context.missing and isinstance(genres, list) and genres:

                if len(genres) > 32:
                    self.http.diagnostics.emit("issue", "invalid_field", field="genre", reason="genre_list_limit")

                ids = list(dict.fromkeys(f"{value}" for value in genres[:32] if type(value) is int and value > 0))

                if ids:
                    names = self.fetch_genre_names()

                    if names is not None:
                        result.fields["genre"] = select_genre(names.get(genre_id) for genre_id in ids)

            results[index] = result

        return results
