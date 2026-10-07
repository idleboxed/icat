"""Public hash recognition and external IDs; no authenticated metadata proxy here."""

import logging
import re
from pathlib import Path

from ..types import LookupContext, Metadata
from .base import MetadataSource
from ..normalization import is_placeholder_title
from .platforms import IGDB
from ...io import paths


logger = logging.getLogger(__name__)
BASE = "https://hasheous.org/api/v1"
# Exact values from Hasheous signature.game, not filename/header guesses.
NES_ARCADE_VARIANTS = frozenset({"VS UniSystem", "PlayChoice-10"})


class HasheousSource(MetadataSource):
    name = "hasheous"
    hosts = frozenset({"hasheous.org"})
    provides = frozenset({"title", "year", "description", "region"})
    identifies = True

    @staticmethod
    def extract_mapping_ids(values: object, source: str, kind: str) -> set[str]:

        if not isinstance(values, list):
            return set()

        return {
            f"{mapping["id"]}"
            for mapping in values
            if isinstance(mapping, dict)
            and mapping.get("source") == source
            and mapping.get("objectType") == kind
            and mapping.get("status") == "Mapped"
            and f"{mapping.get("id", "")}".isdigit()
        }

    def fetch(self, context: LookupContext) -> Metadata:
        rejected: list[dict[str, tuple[object, object]]] = []

        try:
            return self._fetch(context, rejected)

        finally:
            # A full-file VS/PC10 match may resolve an earlier payload-only Arcade
            # candidate. Unresolved failures still survive a later network error.

            for checks in rejected:

                for field, (expected, observed) in checks.items():

                    if field == "platform":
                        observed = [value if len(value) <= 16 else "invalid" for value in observed[:16]]

                    logger.warning(
                        "%s — %s mismatch; ignored (expected %r, got %r)",
                        paths.quote_path(Path(context.rom.entry["image"]).name), field, expected, observed,
                    )
                    self.http.diagnostics.emit(
                        "issue", "identity_mismatch", field=field, expected=expected, observed=observed,
                    )

    def _fetch(self, context: LookupContext, rejected: list[dict[str, tuple[object, object]]]) -> Metadata:
        rom = context.rom

        for digest, size in rom.lookups:
            url = f"{BASE}/Lookup/ByHash/sha1/{digest}"
            candidate = self.get_json(url)

            if candidate is None:
                continue

            signature = self.require_mapping(candidate.get("signature", {}))
            signature_rom = self.require_mapping(signature.get("rom", {}))
            platforms = self.require_mapping(candidate.get("platform", {})).get("metadata")
            signature_sha1 = signature_rom.get("sha1")
            observed_sha1 = (
                signature_sha1.lower()
                if isinstance(signature_sha1, str) and re.fullmatch(r"[0-9a-fA-F]{40}", signature_sha1) else None
            )
            observed_size = signature_rom.get("size")
            mapped = self.extract_mapping_ids(platforms, "IGDB", "Platform")
            checks = {
                "sha1": (digest, observed_sha1),
                "size": (size, observed_size if type(observed_size) is int else None),
                "platform": ([f"{IGDB.get(rom.entry["platform"])}"], sorted(mapped)),
            }
            mismatches = {field: values for field, values in checks.items() if values[0] != values[1]}
            game = signature.get("game")
            variant = game.get("systemVariant") if isinstance(game, dict) else None
            verified_variant = (
                rom.entry["platform"] == "NES"
                and (digest, size) == rom.full_lookup
                and "sha1" not in mismatches and "size" not in mismatches
                and mapped in ({"18"}, {"52"})
                and isinstance(variant, str) and variant in NES_ARCADE_VARIANTS
            )

            if verified_variant:
                mismatches.pop("platform", None)

            if mismatches:
                rejected.append(mismatches)
                continue

            if verified_variant:
                payload_mismatch = {"platform": (["18"], ["52"])}
                recovered = sum(checks == payload_mismatch for checks in rejected)
                rejected[:] = [checks for checks in rejected if checks != payload_mismatch]
                self.http.diagnostics.emit(
                    "identity", "verified_console_variant", variant=variant,
                    basis="full_file_sha1_size", sha1=digest, size=size,
                    platform=sorted(mapped), recovered_payload_candidates=recovered,
                )
                logger.info(
                    "%s — %s verified (full SHA-1 + size)", paths.quote_path(Path(rom.entry["image"]).name), variant,
                )

            game = self.require_mapping(signature.get("game", {}))
            title = self.normalize_text(candidate.get("name"))

            if is_placeholder_title(title):
                return Metadata(fields={"title": title}, sources=[url])

            description = self.normalize_text(game.get("description"), multiline=True)
            result = Metadata(
                fields={
                    "title": title,
                    "year": self.parse_year(game.get("year")),
                    "description": description,
                },
                sources=[url],
            )
            countries = signature_rom.get("country")
            # A game's combined release regions are not the region of this particular ROM.

            if isinstance(countries, dict) and countries:
                values = list(countries.values())

                if "World" in values:
                    result.fields["region"] = []

                else:
                    regions = [
                        {"United States": "USA"}.get(region, region)
                        for region in values if self.normalize_text(region)
                    ]

                    if regions:
                        result.fields["region"] = list(dict.fromkeys(regions))

            for kind, values in (("game", candidate.get("metadata")), ("platform", platforms)):
                ids = self.extract_mapping_ids(values, "TheGamesDb", kind.title())

                if len(ids) == 1:
                    result.identifiers[f"TheGamesDb:{kind}"] = ids.pop()

            if result.fields["title"]:
                result.identifiers["title"] = result.fields["title"]

            return result

        return Metadata()
