# icat

A PC tool for building game catalogues for IGUI (Idle GUI).

The PyPI distribution is `iboxed-icat`; the Python package and CLI command are `icat`.

- Import single-file cartridge ROMs and PlayStation 1 / Sega CD CHD images, including ZIP/7z archives.
- Add titles, descriptions, release years, genres, player counts and thumbnails.
- Preserve existing games, deduplicate identical ROMs and copy sources by default.
- Update metadata and process games marked for deletion in IGUI.

## Install

Requires Python 3.12+. For development, use [makeapp](https://pypi.org/project/makeapp/)
from the source checkout:

```bash
cd /path/to/icat
ma tools
ma up --tool
```

CHD imports need `libchdr`; some older 7z archives need a system `7z` executable.
See the [quickstart](docs/10_quickstart.md) for setup.

## Use

Stop IGUI and other writers before changing the catalogue.

```bash
icat sync --src /path/to/roms --dst /path/to/games
icat sync --help
```

## Documentation

- [Quickstart](docs/10_quickstart.md)
- [Commands and recovery](docs/20_usage.md)
- [Supported formats and limits](docs/30_formats.md)
- [Development](docs/40_development.md)

## License

[BSD-3-Clause](LICENSE). Copyright (c) 2026, Igor Starikov.
