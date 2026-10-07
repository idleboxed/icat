import hashlib
import stat
import zipfile
import zlib
from collections.abc import Callable
from pathlib import Path
import py7zr
import pytest
from icat.errors import CatalogueError
from icat.roms.inspection import inspect_rom
from icat.roms.staging import stage_source


def test_raw_and_zip_have_identical_identity_and_lookup_objects(tmp_path: Path, nes: bytes) -> None:
    raw = tmp_path / "game.nes"
    raw.write_bytes(nes)
    zipped = tmp_path / "game.zip"

    with zipfile.ZipFile(zipped, "w") as archive:
        archive.writestr("nested/Game.nes", nes)
        archive.writestr("readme.txt", "Ignored")

    stage = tmp_path / "stage"
    stage.mkdir()

    result = stage_source(zipped, stage)
    rom = result.roms[0]

    assert rom.entry["hash"] == hashlib.sha256(nes).hexdigest() == inspect_rom(raw, raw.name).entry["hash"]
    assert rom.entry["image"] == "Game.nes"
    assert rom.lookups == [
        (hashlib.sha1(nes[16:]).hexdigest(), len(nes) - 16),
        (hashlib.sha1(nes).hexdigest(), len(nes)),
    ]
    assert rom.crc32s == {
        rom.lookups[0]: f"{zlib.crc32(nes[16:]):08x}",
        rom.lookups[1]: f"{zlib.crc32(nes):08x}",
    }
    assert rom.path.read_bytes() == nes
    assert zipped.exists()


@pytest.mark.parametrize("name", ["../Game.nes", "/Game.nes", "a/../../Game.nes", "C:\\Game.nes"])
def test_unsafe_archive_is_rejected_without_extraction(tmp_path: Path, nes: bytes, name: str) -> None:
    path = tmp_path / "bad.zip"

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(name, nes)

    with pytest.raises(CatalogueError, match="Unsafe ZIP"):
        stage_source(path, tmp_path)

    assert list(tmp_path.iterdir()) == [path]


def test_zip_symlinks_rejected(tmp_path: Path) -> None:
    path = tmp_path / "link.zip"
    info = zipfile.ZipInfo("link.nes")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(info, "somewhere")

    with pytest.raises(CatalogueError, match="ZIP symlink"):
        stage_source(path, tmp_path)


def test_archive_disc_set_is_kept_whole(tmp_path: Path, nes: bytes) -> None:
    path = tmp_path / "set.zip"

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("game.nes", nes)
        archive.writestr("disc.cue", "FILE disc.bin BINARY")

    assert stage_source(path, tmp_path) is None
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("container", ["nes", "zip", "7z"])
def test_legacy_nes_zero_padding_preserves_bytes_but_not_payload_lookup(
    tmp_path: Path, sevenzip_archive: Callable[..., Path], monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, container: str,
) -> None:
    # Same header/sizes as the reported Awful Rushing file; synthetic payload.
    header = b"NES\x1a\x02\x01\x01" + bytes(9)
    payload = b"P" * 32768 + b"C" * 8192
    data = header + payload + bytes(24576)
    path = tmp_path / f"Game.{container}"

    if container == "zip":

        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("Game.nes", data)

    elif container == "7z":
        sevenzip_archive(path, [("Game.nes", data)])

    else:
        path.write_bytes(data)

    original = path.read_bytes()
    monkeypatch.setattr("icat.roms.inspection.CHUNK", 1024)

    result = stage_source(path, tmp_path)

    rom = result.roms[0]
    assert rom.path.read_bytes() == data
    assert rom.entry["hash"] == hashlib.sha256(data).hexdigest()
    assert rom.lookups == [
        (hashlib.sha1(data).hexdigest(), len(data)),
        (hashlib.sha1(payload).hexdigest(), len(payload)),
    ]
    assert rom.crc32s[rom.lookups[0]] == f"{zlib.crc32(data):08x}"
    assert rom.crc32s[rom.lookups[1]] == f"{zlib.crc32(payload):08x}"
    assert path.read_bytes() == original
    assert "24576 trailing bytes" in caplog.text
    assert "nonzero=False" in caplog.text


def test_zip_platform_filter_is_applied_before_archive_selection(tmp_path: Path, nes: bytes) -> None:
    path = tmp_path / "collection.zip"

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Alpha.gb", b"GB")
        archive.writestr("Zelda.nes", nes)

    stage = tmp_path / "stage"
    stage.mkdir()

    result = stage_source(path, stage, platforms=("NES",))

    assert result.roms[0].entry["platform"] == "NES"
    assert result.roms[0].entry["image"] == "Zelda.nes"


def test_markdown_is_not_imported_as_mega_drive_rom(tmp_path: Path, nes: bytes) -> None:
    markdown = tmp_path / "README.md"
    markdown.write_text("# Not a ROM\n")
    path = tmp_path / "game.zip"

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("game.nes", nes)
        archive.writestr("README.md", "# Notes\n")

    assert stage_source(markdown, tmp_path) is None
    assert len(stage_source(path, tmp_path).roms) == 1


@pytest.mark.parametrize("duplicate", [False, True])
def test_names_without_alphanumeric_tokens_skip_zip_without_extracting_any_rom(
    tmp_path: Path, nes: bytes, caplog: pytest.LogCaptureFixture, duplicate: bool,
) -> None:
    path = tmp_path / "collection.zip"

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("!!!.nes", nes)
        archive.writestr("---.nes", nes if duplicate else nes[:-1] + b"D")
        archive.writestr("readme.txt", "Ignored")

    result = stage_source(path, tmp_path)

    assert result is None
    assert list(tmp_path.iterdir()) == [path]
    assert "multiple ROMs" in caplog.text


def test_zip_with_more_than_4096_entries_is_accepted(tmp_path: Path, nes: bytes) -> None:
    path = tmp_path / "many.zip"

    with zipfile.ZipFile(path, "w") as archive:

        for number in range(4180):
            archive.writestr(f"notes/{number}.txt", b"")

        archive.writestr("Game.nes", nes)

    result = stage_source(path, tmp_path)

    assert result.roms[0].path.read_bytes() == nes


@pytest.mark.parametrize("container", ["raw", "zip", "7z"])
def test_rom_larger_than_former_128_mib_limit_is_imported(tmp_path: Path, container: str) -> None:
    # Sparse synthetic input; temporary files belong in the RAM pytest directory.
    raw = tmp_path / "Large.gba"
    size = 128 * 1024 * 1024 + 1

    with raw.open("wb") as stream:
        stream.truncate(size)

    path = raw

    if container == "zip":
        path = tmp_path / "large.zip"

        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(raw, raw.name)

    elif container == "7z":
        path = tmp_path / "large.7z"

        with py7zr.SevenZipFile(path, "w", filters=[{"id": py7zr.FILTER_LZMA2, "preset": 0}]) as archive:
            archive.write(raw, raw.name)

    result = stage_source(path, tmp_path)

    assert result.roms[0].path.stat().st_size == size

    with raw.open("rb") as stream:
        assert result.roms[0].entry["hash"] == hashlib.file_digest(stream, "sha256").hexdigest()
