"""Preflight and verified removal of explicitly identified destination assets.

SPDX-License-Identifier: BSD-3-Clause
"""

from dataclasses import dataclass
from pathlib import Path

from ..errors import CatalogueError
from ..io.files import resolve_relative_file, compute_sha256, get_file_stamp, sync_directory
from ..io import paths


@dataclass(frozen=True)
class TrashFile:
    root: Path
    name: str
    fingerprint: tuple[int, ...]
    digest: str
    label: str = "Trash"

    @property
    def path(self) -> Path:
        """Return the checked path of the file selected for removal."""
        return resolve_relative_file(self.root, self.name)

    def verify(self) -> None:
        """Reject a removal candidate whose identity or digest changed."""
        path = self.path

        if get_file_stamp(path) != self.fingerprint or compute_sha256(path) != self.digest:
            raise CatalogueError(f"{self.label} file changed; refusing removal", path=path)

    def remove(self) -> None:
        """Verify and unlink the selected file, then synchronize its parent directory."""
        self.verify()
        path = self.path
        path.unlink()
        sync_directory(path.parent)


def prepare_trash(
    dst: Path, entries: list[dict], hashes: frozenset[str], *, label: str = "Trash"
) -> list[TrashFile]:
    """Validate files belonging to trashed entries and return their removal plan."""
    files = []

    for entry in entries:
        digest = entry["hash"]

        if digest not in hashes:
            continue

        for root, name, expected in (
            (dst / paths.IMAGES_DIR, entry["image"], digest),
            (dst / paths.THUMBS_DIR, paths.get_thumbnail_name(digest), None),
        ):
            path = resolve_relative_file(root, name)
            # An interrupted cleanup keeps its entries until all files are removed.

            if not path.exists():

                if path.parent.is_dir():
                    sync_directory(path.parent)

                continue

            fingerprint = get_file_stamp(path)
            actual = compute_sha256(path)

            if expected is not None and actual != expected:
                raise CatalogueError(f"{label} image SHA-256 mismatch; refusing removal", path=path)

            item = TrashFile(root, name, fingerprint, actual, label)
            item.verify()
            # Refuse unsupported directory durability before deleting any asset.
            sync_directory(path.parent)
            files.append(item)

    return files
