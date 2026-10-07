"""Built-in source registry. A new source needs a subclass and one registry entry."""

from .hasheous import HasheousSource
from .libretro import LibretroSource
from .libretro_players import LibretroPlayersSource
from .openvgdb import OpenVgdbSource
from .thegamesdb import TheGamesDbSource


SOURCE_TYPES = (HasheousSource, OpenVgdbSource, LibretroPlayersSource, TheGamesDbSource, LibretroSource)
