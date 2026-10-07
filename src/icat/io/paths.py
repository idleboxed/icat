"""ICAT filesystem names; roots supplied by the caller are never replaced.

SD layout is a format shared with IGUI/iman. No repository-relative defaults.
SPDX-License-Identifier: BSD-3-Clause
"""

import json
import os
from pathlib import Path


SOURCE_DIR = Path("roms")
GAMES_DIR = Path("games")
LOGS_DIR = Path("logs")
HOME_CACHE_DIR = ".cache"
APP_CACHE_DIR = "icat"
HTTP_CACHE_DIR = "http"
DATASETS_DIR = "datasets"
JOURNAL_FILE = "journal.json"
PROGRESS_JOURNAL_FILE = "progress.jsonl"
REMOVAL_JOURNAL_FILE = "removals.jsonl"
DATASET_MANIFEST_FILE = "current.json"
CATALOGUE_FILE = "catalogue.json"
PREFERENCES_FILE = "prefs.json"
PREFERENCES_TEMP_FILE = ".prefs.json.tmp"
IMAGES_DIR = "images"
THUMBS_DIR = "thumbs"
LOCK_FILE = ".icat.lock"
SINGLE_ROM_SLOT = "0"
STAGING_PREFIX = ".icat-stage-"
ATOMIC_WRITE_PREFIX = ".icat-"


def quote_path(path: str | Path) -> str:
    """Quote a diagnostic filename without allowing embedded quotes or newlines."""
    return json.dumps(f"{path}", ensure_ascii=False)


def get_image_path(name: str) -> Path:
    """Catalogue image path relative to the destination, not the images directory."""
    return Path(IMAGES_DIR) / name


def get_default_cache_dir() -> Path:
    """Resolve XDG/HOME when requested, not when importing the package."""
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / HOME_CACHE_DIR)) / APP_CACHE_DIR


def get_thumbnail_name(digest: str) -> str:
    return f"{digest[:2]}/{digest}.png"
