from icat.roms.platforms import PLATFORM_EXTENSIONS, PLATFORMS


def test_platform_registry_is_the_single_extension_mapping() -> None:
    assert PLATFORMS == tuple(PLATFORM_EXTENSIONS)
    assert PLATFORM_EXTENSIONS["NES"] == (".nes", ".unf", ".unif")
    assert PLATFORM_EXTENSIONS["MD"] == (".md", ".gen", ".smd", ".bin")
