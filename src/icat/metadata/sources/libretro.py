"""Exact platform-specific Named_Snaps, never a fuzzy cross-platform title search."""

import re
import threading
from pathlib import Path
from urllib.parse import quote

from ...databases.cache import DatasetCache
from ...io.http import HttpClient, NetworkError
from ..types import LookupContext, Metadata
from ...databases.libretro import DatCache
from .libretro_dat import LibretroDatSource
from .platforms import LIBRETRO


class LibretroSource(LibretroDatSource):
    name = "libretro"
    provides = frozenset({"thumbnail"})
    hosts = frozenset({"raw.githubusercontent.com", "api.github.com"})

    def __init__(
        self, http: HttpClient, datasets: DatasetCache, *, key: str | None = None, dat_cache: DatCache | None = None
    ) -> None:
        super().__init__(http, datasets, key=key, dat_cache=dat_cache)
        self.index_lock = threading.Lock()
        self.snapshots: dict[str, set[str] | None] = {}

    def fetch_image_names(self, repository: str) -> set[str] | None:
        # Two bounded Git tree requests per platform, cached by the shared HTTP client.
        # Failure of this optional index must not disable direct image downloads.

        with self.index_lock:

            if repository in self.snapshots:
                return self.snapshots[repository]

            self.snapshots[repository] = None
            base = f"https://api.github.com/repos/libretro-thumbnails/{repository}/git/trees/"

            try:
                root = self.http.get_json(f"{base}master", hosts=self.hosts, limit=1024 * 1024)

                if root is None:
                    return None

                if root.get("truncated") is not False or not isinstance(root.get("tree"), list):
                    raise ValueError("Incomplete thumbnail repository index")

                if any(not isinstance(item, dict) or not isinstance(item.get("path"), str) for item in root["tree"]):
                    raise ValueError("Invalid thumbnail repository entry")

                folders = [item for item in root["tree"] if item["path"] == "Named_Snaps"]

                if not folders:
                    self.snapshots[repository] = set()
                    return set()

                if len(folders) != 1 or folders[0].get("type") != "tree":
                    raise ValueError("Invalid thumbnail directory index")

                digest = folders[0].get("sha")

                if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{40}", digest):
                    raise ValueError("Invalid thumbnail tree identity")

                tree = self.http.get_json(f"{base}{digest}", hosts=self.hosts, limit=8 * 1024 * 1024)

                if tree is None:
                    return None

                if tree.get("truncated") is not False or not isinstance(tree.get("tree"), list):
                    raise ValueError("Incomplete thumbnail file index")

                names = set()

                for item in tree["tree"]:

                    if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                        raise ValueError("Invalid thumbnail index entry")

                    name = item["path"]

                    if "/" in name or "\\" in name:
                        raise ValueError("Unexpected nested thumbnail index entry")

                    if item.get("type") == "blob" and name.endswith(".png"):
                        names.add(name)

                self.snapshots[repository] = names
                return names

            except (NetworkError, OSError, ValueError) as exc:
                self.report_error(exc)
                return None

    def fetch(self, context: LookupContext) -> Metadata:
        platform = context.rom.entry["platform"]

        if platform not in LIBRETRO:
            return Metadata(outcome="unsupported_platform")

        canonical, identity_url = self.find_canonical_name(context)
        title = canonical or Path(context.rom.entry["image"]).stem
        repository = LIBRETRO[platform].replace(" ", "_")
        title = re.sub(r"[&*/:`<>?\\|\"]", "_", title)
        names = self.fetch_image_names(repository)

        if names is not None and f"{title}.png" not in names:
            self.http.diagnostics.emit("thumbnail", "not_listed", name=f"{title}.png")
            return Metadata(outcome="not_found")

        url = (
            f"https://raw.githubusercontent.com/libretro-thumbnails/{repository}/master/Named_Snaps/"
            f"{quote(title, safe="")}.png"
        )
        data = self.get(url, image=True)

        if data:
            return Metadata(thumbnail=data, sources=([identity_url] if identity_url else []) + [url])

        return Metadata()
