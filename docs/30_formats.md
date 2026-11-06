# Supported formats and limits

Each game must fit in one self-contained image. Separate BIOS files are not part
of the game image and are not installed by icat. Extensions are case-insensitive.

## Cartridge images

| Platform ID | Extensions |
|---|---|
| `NES` | `.nes`, `.unf`, `.unif` |
| `FDS` | `.fds` |
| `GB` | `.gb` |
| `GBC` | `.gbc` |
| `GBA` | `.gba` |
| `MD` | `.md`, `.gen`, `.smd`, verified `.bin` |
| `32X` | `.32x` |
| `SMS` | `.sms` |
| `GG` | `.gg` |
| `SG1000` | `.sg` |
| `SNES` | `.sfc`, `.smc` |
| `PCE` | `.pce` |
| `WS` | `.ws` |
| `WSC` | `.wsc` |
| `A2600` | `.a26` |
| `A5200` | `.a52` |
| `A7800` | `.a78` |
| `N64` | `.z64`, `.v64`, `.n64` |
| `NDS` | `.nds` |

- **Mega Drive `.bin`:** needs the `SEGA` signature at offset `0x100`, or an unambiguous
  full-file SHA-1, size and CRC match in the pinned Libretro No-Intro database.
  A signature-free image needs that database, including when rechecking an existing
  catalogue offline. Unknown `.bin` files are rejected; filenames are not evidence.
- **32X:** `.32x` selects the separate `32X` platform by extension, without structural
  validation or conversion. 32X is not autodetected in `.bin` or `.md` files.
- **NES:** iNES headers and PRG/CHR sizes are checked. A confirmed Nestopia database
  match can repair overstated CHR size and a missing battery flag in the temporary
  copy. Use `--no-fix-nes-headers` to disable repair; strict validation remains.
  Repairs change imported bytes and therefore the catalogue hash.

## Metadata lookup

Metadata sources share additional lookup identities for these cartridge representations:

- **SNES:** omit a 512-byte copier header when the remaining image is a positive
  multiple of 8192 bytes. No SNES deinterleaving or padding removal is performed.
- **Mega Drive SMD:** omit the 512-byte header and decode complete 16 KiB interleaved
  blocks. The decoded first block must contain `SEGA` at offset `0x100`.
- **N64:** recognize byte order from the first four bytes, regardless of extension.
  `37 80 40 12` swaps each byte pair; `40 12 37 80` reverses each four-byte word.
  The length must be divisible by the group size. `80 37 12 40` is already canonical.

These transformations only compute extra SHA-1, size and CRC lookup values; they
do not convert the imported ROM. Full-file identity remains available, and stored
bytes, filenames and catalogue SHA-256 are unchanged. Extra lookup values are
matched exactly within the detected platform; no fuzzy title search is added.
Recognition does not verify ROM integrity or emulator compatibility.

Libretro canonical titles and player counts use independent datasets. A missing
player-count dataset does not prevent canonical thumbnail lookup, including for NDS.

## ZIP and 7z

One ROM is selected per archive, after the platform filter. Files are extracted
on the PC; the catalogue stores the image, not the archive.

- A single candidate is accepted directly.
- With multiple cartridge candidates, the normalized archive title must match a
  title group, or all candidates must belong to one title group. Parenthesized and
  bracketed tags, punctuation and case do not affect grouping.
- Within the chosen group, selection prefers an exact archive-name match, then
  `[!]`, then the shortest name and alphabetical order. This is not a region
  preference or proof that a dump is good.
- Ambiguous collections are left untouched, not expanded into multiple games.
- An archive containing CHD must have exactly one candidate image after filtering.
  Archives containing CUE, M3U or SBI are left untouched.

For example, `Example.zip` with `Example (USA).nes` and `Example (Europe).nes`
imports one variant. A generic `Collection.zip` containing unrelated games does not.

## CHD: PlayStation 1 and Sega CD

- Platform IDs: `PSX` and `SCD`. Detection uses disc content; `--platform` only filters it.
- Accepts standalone CD CHD v3–v5 with track metadata and a codec supported by the
  installed `libchdr`. A parent-dependent CHD is not accepted.
- Imported CHD bytes are preserved. There is no conversion to BIN/CUE or full unpacking.
- Identification uses a bounded read of disc service sectors, not a full integrity
  check or proof that the game will run. Copying, hashing and readback still read the whole file.
- CHD titles come from filenames; Libretro thumbnails use an exact title match.
  CHD track-hash metadata lookup is not supported. Container hashes are not Redump track hashes.

Example with a directory of PlayStation 1 CHD images:

```bash
icat sync --src /path/to/discs --dst /path/to/games --platform PSX
```

## Not supported

- Multi-file games, including CUE + BIN/audio and M3U playlists.
- Disc switching and separate SBI files.
- ISO, PBP, other disc platforms, PS2 images and archive formats other than ZIP/7z.

Successful import does not establish emulator compatibility or verify every format's structure.
