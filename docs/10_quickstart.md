# Quickstart

## Requirements

- Python 3.12 or newer.
- [makeapp](https://pypi.org/project/makeapp/).
- A directory of supported ROM files; see [formats and limits](30_formats.md).

Optional system dependencies:

| Dependency | When needed |
|---|---|
| `libchdr` | PlayStation 1 and Sega CD CHD imports. `chdman` is not required. |
| `7z` | Linux fallback for older 7z archives with LZMA settings unsupported by Python. |

On Ubuntu 24.04, install only the packages you need in your terminal:

```bash
sudo apt install libchdr0
sudo apt install 7zip
```

Without a compatible `libchdr`, encountering a CHD stops the operation with an
installation hint. Imports without CHD do not need the library.

## Install

For development, install `iboxed-icat` from the source checkout with makeapp:

```bash
cd /path/to/icat
ma tools
ma up --tool
icat --help
```

Runtime dependencies are installed automatically. The command works from any
directory; keep the checkout in place because this is an editable installation.

## First import

1. Stop IGUI and any other program that writes to the destination.
2. Keep source, destination, cache and log directories separate: none may contain
   another, and none may pass through a symbolic link.
3. Run:

```bash
icat sync --src /path/to/roms --dst /path/to/games
```

The destination gets `catalogue.json`, ROM files under `images/` and PNG thumbnails
under `thumbs/`. Sources are copied, not removed. Existing catalogue entries are
kept; identical ROM content produces one entry.

Network access is needed for uncached metadata, databases and thumbnails.
Do not change the source or destination, or disconnect the destination device,
until the command finishes. See [commands and recovery](20_usage.md) for the next steps.
