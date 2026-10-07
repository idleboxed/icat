"""Read-only inventory of candidate source files."""


import logging
import os
import stat
from collections.abc import Collection
from pathlib import Path

from ..reporting.diagnostics import Diagnostics
from ..io.files import get_file_stamp
from .types import SourceInventory
from .platforms import ARCHIVES, UNSUPPORTED, get_selected_extensions


logger = logging.getLogger(__name__)


def discover_sources(
    root: Path, *, platforms: Collection[str] | None = None, diagnostics: Diagnostics | None = None,
) -> SourceInventory:
    diagnostics = diagnostics or Diagnostics()
    extensions = get_selected_extensions(platforms)
    found = []
    files = 0

    def raise_walk_error(error: OSError) -> None:
        raise error

    for folder, dirs, names in os.walk(root, followlinks=False, onerror=raise_walk_error):

        for name in list(dirs):

            if (Path(folder) / name).is_symlink():
                diagnostics.emit("source", "symlink_directory", path=f"{Path(folder) / name}")
                logger.warning("%s — symlink directory; skipped", Path(folder) / name)
                dirs.remove(name)

        dirs.sort()

        for name in sorted(names):
            path = Path(folder) / name
            suffix = path.suffix.lower()
            mode = path.lstat().st_mode
            files += stat.S_ISREG(mode)

            if stat.S_ISLNK(mode):
                diagnostics.emit("source", "symlink", path=f"{path}")
                logger.warning("%s — symlink; skipped", path)

            elif suffix in extensions or suffix in ARCHIVES:
                get_file_stamp(path)
                found.append(path)

            elif suffix in UNSUPPORTED:
                diagnostics.emit("source", "unsupported_format", path=f"{path}")
                logger.warning("%s — unsupported archive/disc; kept", path)

    return SourceInventory(files, found)
