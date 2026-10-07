"""Operation arguments and validation before acquiring resources."""


from dataclasses import dataclass
from pathlib import Path

from ..errors import CatalogueError
from ..io.files import resolve_relative_file
from ..roms.platforms import get_selected_extensions
from ..io import paths as layout


@dataclass(frozen=True)
class Options:
    src: Path | None
    dst: Path
    cache: Path
    logs: Path
    concurrency: int = 5
    move_roms: bool = False
    trash: bool = False
    trash_reset: bool = False
    fix_nes_headers: bool = True
    platforms: tuple[str, ...] | None = None
    trash_only: bool = False

    @property
    def mode(self) -> str:

        if self.trash_only:
            return "TRASH RESET" if self.trash_reset else "TRASH CLEAN"

        if self.src is None:
            return "METADATA"

        return "MOVE" if self.move_roms else "COPY"

    @property
    def log_command(self) -> str:

        if self.trash_only:
            return "trash-reset" if self.trash_reset else "trash-clean"

        return "metadata" if self.src is None else "sync"

def validate_options(options: Options) -> tuple[Path | None, Path, Path, Path]:
    """Validate roots without creating files; before acquiring operation resources."""

    if not 1 <= options.concurrency <= 32:
        raise CatalogueError("Concurrency must be 1..32")

    if options.trash and options.trash_reset:
        raise CatalogueError("Trash cleanup and trash reset are mutually exclusive")

    if options.trash_only and (options.src is not None or not (options.trash or options.trash_reset)):
        raise CatalogueError("Standalone trash requires no source and exactly one trash action")

    if options.src is None and (options.move_roms or options.platforms):
        raise CatalogueError("Move and platform filters require a source directory")

    get_selected_extensions(options.platforms)
    src = options.src.absolute() if options.src is not None else None
    dst, cache, logs = options.dst.absolute(), options.cache.absolute(), options.logs.absolute()

    if src is not None and (not src.is_dir() or src.is_symlink()):
        raise CatalogueError(
            "Source is not a regular directory", path=src,
            hint="Pass an existing ROM directory with --src. See: icat sync --help",
            impact="No source or catalogue files changed; no run journal created.",
        )

    roots = [dst, cache, logs] if src is None else [src, dst, cache, logs]

    for path in roots:

        if any(parent.is_symlink() for parent in [path, *path.parents]):
            raise CatalogueError("Symlinked paths are not allowed", path=path)

    roots = [path.resolve() for path in roots]

    for index, left in enumerate(roots):

        for right in roots[index + 1:]:

            if left == right or left in right.parents or right in left.parents:
                raise CatalogueError("Source, destination, PC cache and logs must not overlap")

    if src is None and not resolve_relative_file(dst, layout.CATALOGUE_FILE).is_file():
        raise CatalogueError(
            "An existing catalogue is required", path=dst / layout.CATALOGUE_FILE,
            hint="Use icat sync to create a catalogue, or check --dst.",
            impact="No source or catalogue files changed; no run journal created.",
        )

    return (src.resolve() if src is not None else None), dst.resolve(), cache.resolve(), logs.resolve()
