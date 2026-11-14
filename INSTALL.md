# Installing icat

[uv](https://docs.astral.sh/uv/) is recommended for installation, though plain `pip` works too.

## From PyPI

Install the command-line application as an isolated tool (the distribution name is `iboxed-icat`):

```bash
uv tool install iboxed-icat
```

Upgrade it with:

```bash
uv tool upgrade iboxed-icat
```

or with pip:

```bash
pip install iboxed-icat
```

## From source

Clone the repository and synchronize its development environment:

```bash
git clone https://github.com/idlesign/icat icat
cd icat
uv sync
```

Run commands in the project environment with `uv run`.
