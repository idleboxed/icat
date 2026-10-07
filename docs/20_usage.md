# Commands and recovery

## Import games

With `roms/` and `games/` in your current directory:

```bash
icat sync
```

Use `--src` and `--dst` for other locations. Sync adds games; it is not a mirror
and does not remove games missing from the source.

Filter detected platforms, including archive contents:

```bash
icat sync --src /path/to/roms --dst /path/to/games --platform NES,FDS
```

You can repeat `--platform`. It does not force an image to be treated as a platform,
and existing games on other platforms remain in the catalogue.

To delete accepted sources only after the output has been synced and verified:

```bash
icat sync --src /path/to/roms --dst /path/to/games --move-roms
```

**For archives, this deletes the entire accepted archive**, including unselected
variants, filtered platforms and companion files. Sources are rechecked before
removal; rejected sources are kept.

## Update metadata

An existing `catalogue.json` is required; no source directory is used:

```bash
icat metadata --dst /path/to/games
```

Filled fields and existing thumbnails are preserved. Missing fields are filled
where a source can identify the game; otherwise the filename supplies the title
and optional fields remain `null`. Genre names are normalized; unknown genres
become `null` with a warning.

Metadata titled `ZZZ` is ignored, and an existing `ZZZ` title is treated as missing.
ROMs are not removed based on external classifications such as demo or non-game.

## Network and cache

Use only previously cached data, without network requests:

```bash
icat sync --src /path/to/roms --dst /path/to/games --http-offline
icat metadata --dst /path/to/games --http-offline
```

- `--http-refresh` refreshes online metadata without clearing filled catalogue fields.
- `--refresh-db` refreshes persistent databases, keeping the old copy if the update fails.
- `--cache PATH` selects a PC-only cache. The default is `$XDG_CACHE_HOME/icat`,
  or `~/.cache/icat` when that variable is unset.

Built-in sources, in default priority order: Hasheous, OpenVGDB, Libretro player
counts, TheGamesDB and Libretro thumbnails. Choose an explicit order with:

```bash
icat sync --sources hasheous openvgdb libretro-players thegamesdb libretro
```

TheGamesDB needs a key and game/platform identifiers from earlier sources:

```bash
export ICAT_THEGAMESDB_API_KEY="your-key"
icat metadata --dst /path/to/games
```

Without the key, other sources remain available. Keep keys out of the repository
and destination. Online lookups send hashes and platform information to the sources.

## IGUI trash

Trash marks are stored in `prefs.json` next to the `games` directory. These commands
require an existing catalogue and do not import games or fetch metadata:

```bash
icat trash clean --dst /path/to/games
icat trash reset --dst /path/to/games
```

- `clean` deletes marked ROMs, thumbnails and catalogue entries.
- `reset` clears trash marks without deleting games.
- Other preferences and game saves are preserved.

For a combined run, `icat sync --trash` cleans trash before importing;
`icat sync --trash-reset` clears the marks instead. The flags cannot be combined.

## Output files

```text
parent/
  prefs.json                         # IGUI preferences, when present.
  games/
    catalogue.json
    images/<platform>/<prefix>/<rom-file>
    thumbs/<prefix>/<sha256>.png
```

The game ID is the SHA-256 of the complete imported ROM, including its header.
The catalogue's `image` field is relative to `games/images/`.
Existing `games/states/` and `games/saves/` belong to the emulator/session tools:
icat neither creates nor deletes them, even when a game is removed.

## Status and recovery

Messages and progress go to stderr. `--progress plain` selects line-based output;
redirected output uses it automatically. Missing metadata does not mean a rejected ROM.

| Exit code | Meaning |
|---|---|
| `0` | Completed. |
| `1` | Stopped by an error. |
| `2` | Invalid command arguments. |
| `3` | Completed with rejected source files. |
| `130` | Cancelled with Ctrl+C. |

`--logs_dir PATH` selects the log root, defaulting to `logs/` in the current directory.
Each run writes `<logs_dir>/<command>/<UTC-run>/journal.json`; command directories
are `sync`, `metadata`, `trash-clean` and `trash-reset`. Inspect this journal after
an error; early input-validation failures may occur before it is created.

Bad ROMs are rejected individually. I/O errors, destination conflicts and a corrupt
existing catalogue stop the operation. Completed writes and removals are not rolled back.
After an interrupted run, check the journal and confirm that no icat process or
other writer is active before removing leftover `.icat.lock` or `.icat-stage-*`
inside the destination and retrying.

For the full option list, use `icat sync --help`, `icat metadata --help` or
`icat trash clean --help`.
