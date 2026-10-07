import zipfile
from pathlib import Path
from icat.roms.discovery import discover_sources


def test_discovery_filters_raw_roms_but_keeps_archives_for_content_inspection(tmp_path: Path, nes: bytes) -> None:
    selected = tmp_path / "selected.nes"
    selected.write_bytes(nes)
    (tmp_path / "other.gb").write_bytes(b"GB")
    (tmp_path / "other.bin").write_bytes(bytes(512))
    archive = tmp_path / "collection.zip"

    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("Game.nes", nes)

    inventory = discover_sources(tmp_path, platforms=("NES",))

    assert inventory.candidates == [archive, selected]
    assert inventory.files == 4


def test_recursive_filter_ignores_documents_and_symlinks(tmp_path: Path, nes: bytes) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    rom = nested / "game.NES"
    rom.write_bytes(nes)
    (tmp_path / "readme.txt").write_text("not a ROM")
    (tmp_path / "manual.pdf").write_bytes(b"PDF")
    (tmp_path / "linked.nes").symlink_to(rom)
    (tmp_path / "loop").symlink_to(tmp_path, target_is_directory=True)

    inventory = discover_sources(tmp_path)

    assert inventory.candidates == [rom]
    assert inventory.files == 3
