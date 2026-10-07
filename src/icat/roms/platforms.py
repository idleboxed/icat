"""Supported single-file platforms and extension filters."""

from collections.abc import Collection


PLATFORM_EXTENSIONS = {
    "NES": (".nes", ".unf", ".unif"),
    "FDS": (".fds",),
    "GB": (".gb",),
    "GBC": (".gbc",),
    "GBA": (".gba",),
    "MD": (".md", ".gen", ".smd", ".bin"),
    "32X": (".32x",),
    "SMS": (".sms",),
    "GG": (".gg",),
    "SG1000": (".sg",),
    "SNES": (".sfc", ".smc"),
    "PCE": (".pce",),
    "WS": (".ws",),
    "WSC": (".wsc",),
    "A2600": (".a26",),
    "A5200": (".a52",),
    "A7800": (".a78",),
    "N64": (".z64", ".v64", ".n64"),
    "NDS": (".nds",),
    "PSX": (".chd",),
    "SCD": (".chd",),
}
PLATFORMS = tuple(PLATFORM_EXTENSIONS)
EXTENSIONS = {
    extension: platform if extension != ".chd" else None
    for platform, extensions in PLATFORM_EXTENSIONS.items()
    for extension in extensions
}
ARCHIVES = {".zip", ".7z"}
UNSUPPORTED = {".rar", ".gz", ".xz", ".sbi", ".cue", ".m3u", ".iso", ".ccd", ".img", ".pbp"}


def get_selected_extensions(platforms: Collection[str] | None = None) -> dict[str, str | None]:

    if platforms is None:
        return EXTENSIONS

    unknown = set(platforms) - set(PLATFORMS)

    if unknown:
        raise ValueError(f"Unknown platforms: {", ".join(sorted(unknown))}")

    selected = set(platforms)
    return {
        extension: EXTENSIONS[extension]
        for platform in selected for extension in PLATFORM_EXTENSIONS[platform]
    }
