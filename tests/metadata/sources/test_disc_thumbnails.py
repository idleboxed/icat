


from collections.abc import Callable
from ctypes import CDLL
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from icat.databases.cache import DatasetCache
from icat.io.http import HttpClient
from icat.roms.inspection import inspect_rom
from icat.metadata.types import LookupContext
from icat.metadata.sources.hasheous import HasheousSource
from icat.metadata.sources.libretro import LibretroSource
from icat.metadata.sources.libretro_players import LibretroPlayersSource
from icat.metadata.sources.openvgdb import OpenVgdbSource
from icat.metadata.sources.platforms import LIBRETRO
from icat.metadata.thumbnails import convert


@pytest.mark.parametrize("platform", ["PSX", "SCD"])
def test_disc_thumbnails_use_exact_platform_without_cartridge_hash_queries(
    tmp_path: Path, native_chd: CDLL, compressed_chd: dict[str, bytes],
    thumbnail_index_rules: Callable[..., list[str]], response_mock: Callable[..., Any], png: bytes, platform: str,
) -> None:
    path = tmp_path / "Game (Japan).chd"
    path.write_bytes(compressed_chd[platform])
    rom = inspect_rom(path, path.name)
    http = HttpClient(tmp_path / "cache")
    cache = DatasetCache(tmp_path / "datasets")
    context = LookupContext(rom, dict(rom.entry))
    repository = LIBRETRO[platform].replace(" ", "_")
    url = (
        f"https://raw.githubusercontent.com/libretro-thumbnails/{repository}/master/Named_Snaps/{quote(path.stem)}.png"
    )

    with response_mock([*thumbnail_index_rules(platform), f"GET {url} -> 200 :".encode() + png]):
        # These providers must not mistake CHD header/raw/container SHA-1 for a disc track identity.
        assert not HasheousSource(http, cache).fetch(context).fields
        assert not OpenVgdbSource(http, cache).fetch(context).fields
        assert not LibretroPlayersSource(http, cache).fetch(context).fields
        result = LibretroSource(http, cache).fetch(context)

    assert result.thumbnail == convert(png)
