"""Deterministic fill-missing orchestration, independent of source-specific formats."""

from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import Executor
from contextlib import closing
from dataclasses import replace

from ..databases.cache import DatasetCache
from ..reporting.diagnostics import Diagnostics
from ..databases.libretro import DatCache, LibretroDatDatabase
from ..roms.formats.mega_drive import MegaDriveIdentifier
from ..io.http import HttpClient
from ..reporting.progress import ProviderProgress
from ..roms.types import Rom
from .sources import SOURCE_TYPES
from .types import LookupContext, Metadata
from .sources.base import MetadataSource
from .normalization import is_description_missing, is_placeholder_title
from .sources.libretro_dat import LibretroDatSource
from ..io import paths


__all__ = ["Provider", "build_provider"]


class Provider:
    def __init__(
        self, sources: Sequence[MetadataSource], *, md_identifier: MegaDriveIdentifier | None = None,
    ) -> None:
        self.md_identifier = md_identifier
        self.sources = tuple(sources)
        self.diagnostics = self.sources[0].http.diagnostics if self.sources else Diagnostics()

        if md_identifier is not None:
            md_identifier.database.http.diagnostics = self.diagnostics
            md_identifier.database.datasets.diagnostics = self.diagnostics

        for source in self.sources:
            source.http.diagnostics = self.diagnostics
            source.datasets.diagnostics = self.diagnostics

    def cancel(self) -> None:

        if self.md_identifier is not None:
            self.md_identifier.database.http.cancel()

        for source in self.sources:
            source.http.cancel()

    def get_settings(self) -> dict:
        return {
            "sources": [
                {"name": source.name, "key_available": bool(source.key),
                 "refresh_databases": source.datasets.refresh, "batch_size": source.batch_size}
                for source in self.sources
            ],
            "network": self.sources[0].http.get_settings() if self.sources else {},
            "metadata_batch_size": self.batch_size,
        }

    def lookup(self, rom: Rom) -> Metadata:
        return self.lookup_many([rom])[0]

    @property
    def batch_size(self) -> int:
        """Largest enabled source batch, not a pre-filter chunk of the whole catalogue."""
        sizes = [source.batch_size for source in self.sources if not source.key_env or source.key]

        if any(type(size) is not int or not 1 <= size <= 128 for size in sizes):
            raise ValueError("Source batch size must be 1..128")

        return max(sizes, default=1)

    def lookup_many(self, roms: Sequence[Rom]) -> list[Metadata]:
        results = [Metadata() for _rom in roms]

        for index, result in self.iter_lookup(roms):
            results[index] = result

        return results

    def iter_lookup(
        self, roms: Sequence[Rom], *, executor: Executor | None = None, max_pending: int = 1,
    ) -> Iterator[tuple[int, Metadata]]:
        """Finish sources in priority order, packing eligible identities across all ROMs."""
        contexts = []

        for rom in roms:
            values = dict(rom.entry)

            if is_description_missing(values):
                values["description"] = None

            if is_placeholder_title(values.get("title")):
                values["title"] = None

            contexts.append(LookupContext(rom, values, has_thumbnail=rom.has_thumbnail))

        combined = [Metadata() for _context in contexts]
        progress = None
        try:

            if not self.sources:
                yield from enumerate(combined)

            for position, source in enumerate(self.sources):
                progress = ProviderProgress(source.name, position + 1, len(self.sources), 0, len(contexts))
                progress.emit()

                with closing(source.iter_lookup(
                    contexts, executor=executor, max_pending=max_pending,
                )) as results:

                    for index, result in results:
                        progress = replace(progress, completed=progress.completed + 1)
                        progress.emit()
                        context, target = contexts[index], combined[index]

                        for name, value in result.fields.items():

                            if name in context.missing:
                                context.values[name] = value
                                context.resolved.add(name)
                                target.fields[name] = value
                                target.field_sources[name] = result.field_sources[name]

                        for name, value in result.identifiers.items():
                            context.identifiers.setdefault(name, value)
                            target.identifiers.setdefault(name, value)

                        if result.thumbnail is not None and not context.has_thumbnail:
                            target.thumbnail = result.thumbnail
                            target.field_sources["thumbnail"] = source.name
                            context.has_thumbnail = True

                        target.sources.extend(url for url in result.sources if url not in target.sources)

                        if position == len(self.sources) - 1:
                            yield index, target

                status = "disabled" if source.key_env and not source.key else "done"
                replace(progress, status=status if contexts else "not_needed").emit()

        except BaseException as exc:
            self.cancel()

            if progress is not None:
                status = "interrupted" if isinstance(exc, (KeyboardInterrupt, GeneratorExit)) else "failed"
                replace(progress, status=status).emit()

            raise


def build_provider(
    http: HttpClient,
    *,
    environ: Mapping[str, str],
    refresh_databases: bool = False,
    source_names: Sequence[str] | None = None,
) -> Provider:
    cache = DatasetCache(
        http.cache / paths.DATASETS_DIR, offline=http.offline, refresh=refresh_databases, diagnostics=http.diagnostics
    )
    registry = {cls.name: cls for cls in SOURCE_TYPES}
    selected = list(registry) if source_names is None else list(source_names)

    if len(selected) != len(set(selected)) or any(name not in registry for name in selected):
        raise ValueError("Unknown or duplicate metadata sources")

    dat_cache = DatCache()
    return Provider(
        [
            registry[name](
                http, cache, key=environ.get(registry[name].key_env) if registry[name].key_env else None,
                **({"dat_cache": dat_cache} if issubclass(registry[name], LibretroDatSource) else {}),
            )
            for name in selected
        ],
        md_identifier=MegaDriveIdentifier(LibretroDatDatabase(http, cache, dat_cache=dat_cache)),
    )
