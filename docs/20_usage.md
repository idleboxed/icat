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

Run the same command to fill missing thumbnails. [Lookup identities](30_formats.md#metadata-lookup)
are computed when needed and reused from the PC cache, without rereading every ROM.
Neither `metadata` nor `sync` fully audits existing games; imported copies and MOVE
destinations still receive SHA verification. No reimport or cache deletion is needed;
stored ROMs and catalogue IDs remain unchanged. An identified game may still lack a snapshot.

Metadata titled `ZZZ` is ignored, and an existing `ZZZ` title is treated as missing.
ROMs are not removed based on external classifications such as demo or non-game.

## Network and cache

Use only previously cached data, without network requests:

```bash
icat sync --src /path/to/roms --dst /path/to/games --http-offline
icat metadata --dst /path/to/games --http-offline
```

- `--http-refresh` refreshes online metadata, including cached HTTP misses, without
  clearing filled catalogue fields. It is optional for stale responses, not required
  to compute additional lookup identities.
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

For `metadata`, progress shows catalogue records, skipped ROMs, lookup candidates
and records with prepared updates, without unused source-file counters. Prepared
updates are not saved until the catalogue is written and verified. Warnings and
errors stay in the diagnostic output; the live panel does not repeat them.

| Exit code | Meaning |
|---|---|
| `0` | Completed. |
| `1` | Stopped by an error. |
| `2` | Invalid command arguments. |
| `3` | Completed with rejected sources or skipped indexed ROMs. |
| `130` | Cancelled with Ctrl+C. |

`--logs_dir PATH` selects the log root, defaulting to `logs/` in the current directory.
Each run writes `<logs_dir>/<command>/<UTC-run>/journal.json`; command directories
are `sync`, `metadata`, `trash-clean` and `trash-reset`. Inspect this journal after
an error; early input-validation failures may occur before it is created.

Run directory names use `YYYY-MM-DDTHH-MM-SSZ`, for example `2026-11-07T08-23-26Z`.
They contain no colons, so they are valid on FAT/exFAT too. Timestamps inside the
journal retain ISO 8601 format. Existing run directories are not renamed.

The `thumbnail.filename_fallback` counter under `diagnostics.counts.libretro`
counts exact filename-based thumbnail lookups without a verified canonical title.
It does not mean that the whole platform lacks snapshots.

Bad source ROMs are rejected individually. Both `sync` and `metadata` skip missing,
empty and other detected invalid indexed ROMs, preserving their records and files;
reasons are in `skipped_images` in the run journal. MOVE retains related sources.
I/O errors, unsafe paths, catalogue conflicts, detected file changes and publication
failures still stop the operation. Completed writes and removals are not rolled back.

Stop IGUI and other writers before running icat, and do not run multiple icat
operations on the same destination at once.

Temporary `.icat-stage-*` directories are needed only when importing source ROMs.
They are removed after publication, on an error or on Ctrl+C; metadata-only and
trash-only operations do not create them. A forced kill or a cleanup I/O error
can still leave staging files behind. After an interrupted run, check the journal
and confirm that no icat process or other writer is active before removing such
leftovers inside the destination and retrying.

For the full option list, use `icat sync --help`, `icat metadata --help` or
`icat trash clean --help`.
