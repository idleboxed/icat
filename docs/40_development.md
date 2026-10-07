# Development

## Local checks

Use Python 3.12 and [makeapp](https://pypi.org/project/makeapp/) for local development.
Run from the package directory:

```bash
ma tools
ma up --tool
ma tests
```

The local matrix comes from `.github/workflows/python-package.yml`; GitHub Actions
runs pytest directly through uv. Tests use synthetic data without real hardware
or external network access. Native CHD checks are skipped without a compatible
`libchdr`; `chdman` is not needed to run them.

## Source layout

`src/icat/cli.py` handles arguments, presentation and exit codes. Other code is
grouped by responsibility:

| Package | Responsibility |
|---|---|
| `operations` | Session resources and import, metadata, trash and publication phases. |
| `catalogue` | Catalogue validation, preferences and trash plans. |
| `roms` | Discovery, inspection and staging; nested `archives` and `formats`. |
| `metadata` | Providers, normalization and thumbnails; nested external `sources`. |
| `databases` | Dataset cache, DAT parsing and shared Libretro indexes. |
| `io` | File operations, paths and HTTP. |
| `reporting` | Diagnostics, operation journals and progress display. |

Tests mirror these packages under `tests/`; `tests/integration/` exercises components
together with temporary files and substituted network responses.
Follow the root `AGENTS.md` and `IDLE.md` when changing the project.

## Packaging

The distribution name is `iboxed-icat`; the Python package and CLI command are `icat`.

```bash
uv build
```

Wheel: the `icat` package, metadata and license. Sdist: source, tests, the test
workflow, documentation and its configuration, README, license and build metadata.
Hatchling uses the explicit target file lists and respects `.gitignore`.

Keep environments, caches, `uv.lock`, generated sites, secrets, ROMs, firmware and
run outputs out of both the repository and distributions. Check archive contents
before publishing; `.gitignore` cannot remove already tracked files or secrets from history.
