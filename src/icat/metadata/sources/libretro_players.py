"""Players from a coherent Libretro DAT snapshot, joined through verified ROM identities."""

from ..types import LookupContext, Metadata
from .libretro_dat import SYSTEMS, LibretroDatSource


class LibretroPlayersSource(LibretroDatSource):
    name = "libretro-players"
    provides = frozenset({"players"})

    def fetch(self, context: LookupContext) -> Metadata:
        platform = context.rom.entry["platform"]

        if platform not in SYSTEMS:
            return Metadata(outcome="unsupported_platform")

        # One parse per table/run; immutable indexes are shared by all ROM workers.

        with self.lock:
            identities, identity_url = self.get_table(platform, "no-intro")

            if identities is None:
                return Metadata()

            players, players_url = self.get_table(platform, "maxusers")

            if players is None:
                return Metadata()

        counts = set()
        matched = self.find_matching_identities(context, identities)

        if matched is None:
            return Metadata()

        for rom_id in matched:
            crc = context.rom.crc32s[rom_id]
            counts.update(players.players.get(crc, set()))
            counterparts = identities.counterparts.get(rom_id, set())

            if len(counterparts) > 1:
                self.http.diagnostics.emit("issue", "ambiguous_headered_identity")
                return Metadata()

            for full_id in counterparts:
                full_crcs = identities.identities.get(full_id, set())

                if len(full_crcs) != 1:
                    self.http.diagnostics.emit("issue", "ambiguous_headered_crc")
                    return Metadata()

                full_crc = next(iter(full_crcs))

                if identities.owners.get(full_crc) != {full_id}:
                    self.http.diagnostics.emit("issue", "ambiguous_headered_crc")
                    return Metadata()

                counts.update(players.players.get(full_crc, set()))

        if len(counts) != 1 or None in counts:
            return Metadata()

        return Metadata(fields={"players": counts.pop()}, sources=[identity_url, players_url])
