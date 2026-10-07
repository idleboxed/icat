"""Synthetic 7z archives only: public import/CLI behavior, no hardware or network.

SPDX-License-Identifier: BSD-3-Clause
"""

import hashlib
import json
import lzma
import struct
import zipfile
import zlib
from collections.abc import Callable, Sequence
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from typing import Any

import py7zr
import pytest
from py7zr.exceptions import UnsupportedCompressionMethodError
from py7zr.io import WriterFactory

from icat.errors import CatalogueError
from icat.catalogue.codec import decode
from icat.cli import main
from icat.metadata.types import Metadata
from icat.roms.types import Rom
from icat.roms.discovery import discover_sources
from icat.roms.inspection import inspect_rom
from icat.roms.staging import stage_source
from icat.operations.options import Options
from icat.operations.session import run


def test_7z_platform_filter_is_applied_before_archive_selection(
    tmp_path: Path, nes: bytes, sevenzip_archive: Callable[..., Path],
) -> None:
    path = sevenzip_archive(tmp_path / "collection.7z", [("Alpha.gb", b"GB"), ("Zelda.nes", nes)])
    stage = tmp_path / "stage"
    stage.mkdir()

    source = stage_source(path, stage, platforms=("NES",))

    assert source.roms[0].entry["platform"] == "NES"
    assert source.roms[0].entry["image"] == "Zelda.nes"


@pytest.mark.parametrize("method", [py7zr.FILTER_LZMA, py7zr.FILTER_LZMA2, py7zr.FILTER_COPY])
def test_7z_and_raw_have_identical_bytes_identity_and_lookups(
    tmp_path: Path, nes: bytes, sevenzip_archive: Callable[..., Path], method: int,
) -> None:
    raw = tmp_path / "Game.NES"
    raw.write_bytes(nes)
    path = sevenzip_archive(
        tmp_path / "Game.7Z",
        [("notes/readme.txt", b"Ignored"), ("nested/Game.NES", nes)],
        filters=[{"id": method}],
    )
    stage = tmp_path / "stage"
    stage.mkdir()

    source = stage_source(path, stage)

    actual, expected = source.roms[0], inspect_rom(raw, raw.name)
    assert actual.entry == expected.entry
    assert actual.lookups == expected.lookups
    assert actual.crc32s == expected.crc32s
    assert actual.path.read_bytes() == nes
    assert list(stage.iterdir()) == [actual.path]
    assert source.digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert path in discover_sources(tmp_path).candidates


@pytest.mark.parametrize("mega_drive", [False, True])
def test_solid_archive_probes_markdown_headers_and_imports_only_a_cartridge(
    tmp_path: Path, nes: bytes, sevenzip_archive: Callable[..., Path], mega_drive: bool,
) -> None:
    data = bytes(256) + b"SEGA" + bytes(1024) if mega_drive else nes
    name = "Game.MD" if mega_drive else "Game.nes"
    path = sevenzip_archive(
        tmp_path / "game.7z",
        [("README.md", b"# Notes\n" * 200), ("docs/README.MD", b"# More notes"), (name, data)],
    )

    result = stage_source(path, tmp_path)

    assert result.roms[0].path.read_bytes() == data
    assert result.roms[0].entry["image"] == name
    assert result.roms[0].entry["platform"] == ("MD" if mega_drive else "NES")


def test_multiple_solid_blocks_and_directory_entries(tmp_path: Path, nes: bytes) -> None:
    folder = tmp_path / "nested"
    folder.mkdir()
    path = tmp_path / "blocks.7z"

    with py7zr.SevenZipFile(path, "w") as archive:
        archive.write(folder, "nested")
        archive.writestr(b"# Notes", "nested/README.md")

    with py7zr.SevenZipFile(path, "a") as archive:
        archive.writestr(nes, "nested/Game.nes")

    source = stage_source(path, tmp_path)

    assert source.roms[0].path.read_bytes() == nes
    assert not list(folder.iterdir())


@pytest.mark.parametrize("duplicate", [False, True])
def test_names_without_alphanumeric_tokens_skip_the_whole_archive_before_extraction(
    tmp_path: Path, nes: bytes, sevenzip_archive: Callable[..., Path],
    caplog: pytest.LogCaptureFixture, duplicate: bool,
) -> None:
    path = sevenzip_archive(
        tmp_path / "collection.7z", [("!!!.nes", nes), ("---.nes", nes if duplicate else nes[:-1] + b"D")]
    )

    assert stage_source(path, tmp_path) is None

    assert list(tmp_path.iterdir()) == [path]
    assert "multiple ROMs" in caplog.text


def test_mixed_platform_collection_without_matching_title_is_kept(
    tmp_path: Path, nes: bytes, sevenzip_archive: Callable[..., Path], caplog: pytest.LogCaptureFixture,
) -> None:
    path = sevenzip_archive(
        tmp_path / "collection.7z", [("one.nes", nes), ("two.md", bytes(256) + b"SEGA" + bytes(256))]
    )

    source = stage_source(path, tmp_path)

    assert source is None
    assert path.is_file()
    assert "do not unambiguously match the archive name; kept" in caplog.text


@pytest.mark.parametrize("name", ["disc.cue", "disc.iso", "nested.zip", "nested.7z", "nested.rar"])
def test_disc_sets_and_nested_archives_are_kept_whole(
    tmp_path: Path, nes: bytes, sevenzip_archive: Callable[..., Path], name: str,
) -> None:
    path = sevenzip_archive(tmp_path / "set.7z", [("Game.nes", nes), (name, b"unsupported")])

    assert stage_source(path, tmp_path) is None

    assert list(tmp_path.iterdir()) == [path]


def test_zip_with_nested_7z_is_still_kept_whole(tmp_path: Path, nes: bytes) -> None:
    path = tmp_path / "nested.zip"

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Game.nes", nes)
        archive.writestr("nested.7z", b"unsupported nested container")

    assert stage_source(path, tmp_path) is None

    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("entries", [[], [("readme.txt", b"Notes")], [("readme.md", b"# Notes")]])
def test_no_cartridge_keeps_archive_without_creating_output(
    tmp_path: Path, sevenzip_archive: Callable[..., Path], entries: list[tuple[str, bytes]],
) -> None:
    path = sevenzip_archive(tmp_path / "notes.7z", entries)

    assert stage_source(path, tmp_path) is None

    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize(
    "name", ["../Game.nes", "/Game.nes", "C:\\Game.nes", "a/../../Game.nes", "a//Game.nes", "./Game.nes"]
)
def test_unsafe_paths_are_rejected_before_output(
    tmp_path: Path, nes: bytes, sevenzip_archive: Callable[..., Path], name: str,
) -> None:
    # Rewrite a plain synthetic 7z filename and its header CRCs. The library's
    # writer rejects unsafe names, so use bytes rather than patching its internals.
    placeholder = "x" * len(name)
    path = sevenzip_archive(tmp_path / "unsafe.7z", [(placeholder, nes)])
    data = bytearray(path.read_bytes())
    start = 32 + struct.unpack_from("<Q", data, 12)[0]
    header = data[start:]
    old = placeholder.encode("utf-16-le") + b"\0\0"
    assert header.count(old) == 1
    header = header.replace(old, name.encode("utf-16-le") + b"\0\0")
    data[start:] = header
    struct.pack_into("<I", data, 28, zlib.crc32(header))
    struct.pack_into("<I", data, 8, zlib.crc32(data[12:32]))
    path.write_bytes(data)

    with pytest.raises(CatalogueError, match="Unsafe 7z"):
        stage_source(path, tmp_path)

    assert list(tmp_path.iterdir()) == [path]


def test_duplicate_names_are_rejected(tmp_path: Path, nes: bytes, sevenzip_archive: Callable[..., Path]) -> None:
    path = sevenzip_archive(tmp_path / "duplicate.7z", [("Game.nes", nes), ("Game.nes", nes)])

    with pytest.raises(CatalogueError, match="Duplicate 7z member"):
        stage_source(path, tmp_path)

    assert list(tmp_path.iterdir()) == [path]


def test_symlinks_are_rejected_even_when_they_are_not_roms(tmp_path: Path, nes: bytes) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched")
    link = tmp_path / "link"
    link.symlink_to(outside)
    path = tmp_path / "link.7z"

    with py7zr.SevenZipFile(path, "w") as archive:
        archive.write(link, "notes.txt")
        archive.writestr(nes, "Game.nes")

    with pytest.raises(CatalogueError, match="7z symlink or special file"):
        stage_source(path, tmp_path)

    assert set(tmp_path.iterdir()) == {path, link, outside}
    assert outside.read_text() == "untouched"


@pytest.mark.parametrize("header_encryption", [False, True])
def test_encrypted_archives_are_rejected(tmp_path: Path, nes: bytes, header_encryption: bool) -> None:
    path = tmp_path / "encrypted.7z"

    with py7zr.SevenZipFile(path, "w", password="synthetic-test", header_encryption=header_encryption) as archive:
        archive.writestr(nes, "Game.nes")

    with pytest.raises(CatalogueError, match="Encrypted 7z archive"):
        stage_source(path, tmp_path)

    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("extra", [False, True])
def test_ignored_solid_members_do_not_prevent_import(
    tmp_path: Path, nes: bytes, sevenzip_archive: Callable[..., Path], extra: bool,
) -> None:
    entries = [("Game.nes", nes)]

    if extra:
        entries.insert(0, ("manual.txt", b"Ignored but decompressed"))

    path = sevenzip_archive(tmp_path / "large.7z", entries)

    result = stage_source(path, tmp_path)

    assert result.roms[0].path.read_bytes() == nes


def test_solid_decoding_has_no_total_size_limit_even_with_markdown_probe(tmp_path: Path, nes: bytes) -> None:
    ignored = tmp_path / "manual.txt"

    with ignored.open("wb") as stream:
        stream.truncate(128 * 1024 * 1024 + 1)

    path = tmp_path / "large-solid.7z"

    with py7zr.SevenZipFile(path, "w", filters=[{"id": py7zr.FILTER_LZMA2, "preset": 0}]) as archive:
        archive.write(ignored, ignored.name)
        archive.writestr(b"# Notes", "README.md")
        archive.writestr(nes, "Game.nes")

    result = stage_source(path, tmp_path)

    assert result.roms[0].path.read_bytes() == nes
    assert path.is_file()


@pytest.mark.parametrize(
    ("name", "data", "error"),
    [("Game.nes", b"", "Empty"), ("Game.nes", b"bad", "iNES"), ("Game.bin", bytes(512), "Ambiguous")],
)
def test_empty_and_invalid_roms_are_rejected(
    tmp_path: Path, sevenzip_archive: Callable[..., Path], name: str, data: bytes, error: str,
) -> None:
    path = sevenzip_archive(tmp_path / "bad.7z", [(name, data)])

    with pytest.raises(CatalogueError, match=error):
        stage_source(path, tmp_path)

    assert path.is_file()


def test_more_than_4096_entries_are_accepted(
    tmp_path: Path, nes: bytes, sevenzip_archive: Callable[..., Path],
) -> None:
    entries = [(f"notes/{number}.txt", b"") for number in range(4180)] + [("Game.nes", nes)]
    path = sevenzip_archive(tmp_path / "many.7z", entries)

    result = stage_source(path, tmp_path)

    assert result.roms[0].path.read_bytes() == nes
    assert path.is_file()


@pytest.mark.parametrize("declared_delta, error", [(-1, "exceeds declared size"), (1, "Incomplete 7z extraction")])
def test_actual_output_must_match_declared_size(
    tmp_path: Path, nes: bytes, sevenzip_archive: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch, declared_delta: int, error: str,
) -> None:
    path = sevenzip_archive(tmp_path / "lying.7z", [("Game.nes", nes)])
    original = py7zr.SevenZipFile.list

    def misreport_sizes(archive: py7zr.SevenZipFile) -> list[Any]:
        return [replace(member, uncompressed=member.uncompressed + declared_delta) for member in original(archive)]

    monkeypatch.setattr(py7zr.SevenZipFile, "list", misreport_sizes)

    with pytest.raises(CatalogueError, match=error):
        stage_source(path, tmp_path)

    assert path.is_file()


@pytest.mark.parametrize("damage", ["magic", "truncated", "crc", "header"])
def test_corrupt_7z_is_retained_and_cli_continues_with_later_roms(
    options: Options, nes: bytes, sevenzip_archive: Callable[..., Path], caplog: pytest.LogCaptureFixture, damage: str,
) -> None:
    path = sevenzip_archive(options.src / "bad.7z", [("Game.nes", nes)], filters=[{"id": py7zr.FILTER_COPY}])
    data = bytearray(path.read_bytes())

    if damage == "magic":
        data[0] ^= 1

    elif damage == "truncated":
        data = data[:12]

    elif damage == "header":
        start = 32 + struct.unpack_from("<Q", data, 12)[0]
        data[start] = 255
        struct.pack_into("<I", data, 28, zlib.crc32(data[start:]))
        struct.pack_into("<I", data, 8, zlib.crc32(data[12:32]))

    else:
        data[32 + 100] ^= 1

    path.write_bytes(data)
    later = options.src / "z.nes"
    later.write_bytes(nes)

    result = main(
        [
            "sync",
            "--http-offline",
            "--move-roms",
            "--src",
            f"{options.src}",
            "--dst",
            f"{options.dst}",
            "--cache",
            f"{options.cache}",
        ]
    )

    assert result == 3
    assert "7z" in caplog.text and "Invalid" in caplog.text
    assert path.read_bytes() == data
    entries = decode((options.dst / "catalogue.json").read_bytes())
    assert len(entries) == 1
    assert (options.dst / "images" / entries[0]["image"]).read_bytes() == nes
    assert not later.exists()
    assert not list(options.dst.glob(".icat-stage-*"))
    assert not (options.dst / ".icat.lock").exists()


@pytest.mark.parametrize("failure", [OSError("disk full"), KeyboardInterrupt()])
def test_extraction_failure_or_cancellation_retains_all_sources_and_cleans_staging(
    options: Options, provider: Any, nes: bytes, sevenzip_archive: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch, failure: BaseException,
) -> None:
    path = sevenzip_archive(options.src / "game.7z", [("Game.nes", nes)])
    raw = options.src / "a.nes"
    raw.write_bytes(nes)
    original = path.read_bytes()

    def fail(archive: py7zr.SevenZipFile, *, targets: Sequence[str], factory: WriterFactory) -> None:
        factory.create(targets[0]).write(nes[:100])
        raise failure

    monkeypatch.setattr(py7zr.SevenZipFile, "extract", fail)

    with pytest.raises(type(failure), match="disk full" if isinstance(failure, OSError) else "^$"):
        run(replace(options, move_roms=True), provider)

    assert path.read_bytes() == original
    assert raw.read_bytes() == nes
    assert not (options.dst / "catalogue.json").exists()
    assert not list(options.dst.glob(".icat-stage-*"))
    assert not (options.dst / ".icat.lock").exists()
    assert not list((options.dst / "images").iterdir())


def test_lzma_error_uses_bounded_external_decoder_for_selected_member(
    tmp_path: Path, nes: bytes, sevenzip_archive: Callable[..., Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = sevenzip_archive(tmp_path / "Game.7z", [("Game.nes", nes)])
    stage = tmp_path / "stage"
    stage.mkdir()
    calls = []

    def raise_unsupported_codec(
        archive: py7zr.SevenZipFile, *, targets: Sequence[str], factory: WriterFactory,
    ) -> None:
        factory.create(targets[0]).write(nes[:100])
        raise lzma.LZMAError("Unsupported LZMA properties")

    class Process:
        def __init__(self, command: list[str], **kwargs: Any) -> None:
            calls.append((command, kwargs))
            self.stdout = BytesIO(nes)

        def wait(self) -> int:
            return 0

        def kill(self) -> None:
            pytest.fail("Successful decoder must not be killed")

    def find_7z_executable(name: str) -> str:
        return "/usr/bin/7z"

    monkeypatch.setattr(py7zr.SevenZipFile, "extract", raise_unsupported_codec)
    monkeypatch.setattr("icat.roms.archives.sevenzip.shutil.which", find_7z_executable)
    monkeypatch.setattr("icat.roms.archives.sevenzip.subprocess.Popen", Process)

    source = stage_source(path, stage)

    assert source.roms[0].path.read_bytes() == nes
    command, kwargs = calls[0]
    assert command[:4] == ["/usr/bin/7z", "x", "-so", "--"]
    assert command[-1] == "Game.nes"
    assert command[-2].startswith("/proc/self/fd/")
    assert kwargs["pass_fds"] and kwargs["stderr"] == -3


def test_lzma_error_without_system_7z_has_precise_public_error(
    tmp_path: Path, nes: bytes, sevenzip_archive: Callable[..., Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = sevenzip_archive(tmp_path / "Game.7z", [("Game.nes", nes)])
    stage = tmp_path / "stage"
    stage.mkdir()

    def raise_unsupported_codec(
        archive: py7zr.SevenZipFile, *, targets: Sequence[str], factory: WriterFactory,
    ) -> None:
        raise lzma.LZMAError("Unsupported LZMA properties")

    def find_7z_executable(name: str) -> None:
        return None

    monkeypatch.setattr(py7zr.SevenZipFile, "extract", raise_unsupported_codec)
    monkeypatch.setattr("icat.roms.archives.sevenzip.shutil.which", find_7z_executable)

    with pytest.raises(CatalogueError, match="System 7z is required.*Game.nes"):
        stage_source(path, stage)


def test_unsupported_7z_codec_keeps_archive_but_continues_import(
    options: Options, provider: Any, nes: bytes,
    sevenzip_archive: Callable[..., Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = sevenzip_archive(options.src / "a.7z", [("Game.nes", nes)])
    original = path.read_bytes()
    later = options.src / "z.nes"
    later.write_bytes(nes)

    def raise_unsupported_codec(
        archive: py7zr.SevenZipFile, *, targets: Sequence[str], factory: WriterFactory,
    ) -> None:
        factory.create(targets[0]).write(nes[:100])
        raise UnsupportedCompressionMethodError(None, "unsupported")

    monkeypatch.setattr(py7zr.SevenZipFile, "extract", raise_unsupported_codec)

    result = run(replace(options, move_roms=True), provider)

    assert (result.total, result.removed, result.rejected) == (1, 1, 1)
    assert path.read_bytes() == original
    assert not later.exists()
    assert not list(options.dst.glob(".icat-stage-*"))


def test_copy_then_move_deduplicates_raw_zip_and_7z_and_journals_archive_hash(
    options: Options, provider: Any, nes: bytes, sevenzip_archive: Callable[..., Path],
) -> None:
    raw = options.src / "Game.nes"
    raw.write_bytes(nes)
    path = sevenzip_archive(options.src / "game.7z", [("nested/Renamed.nes", nes), ("readme.txt", b"Notes")])

    with zipfile.ZipFile(options.src / "game.zip", "w") as archive:
        archive.writestr("Another.nes", nes)

    original = path.read_bytes()

    first = run(options, provider)
    assert first.total == 1 and first.removed == 0
    assert path.read_bytes() == original
    second = run(replace(options, move_roms=True), provider)

    assert second.total == 1 and second.removed == 3 and second.imported == 0
    assert not list(options.src.iterdir())
    entry = decode((options.dst / "catalogue.json").read_bytes())[0]
    assert (options.dst / "images" / entry["image"]).read_bytes() == nes
    journals = [json.loads(journal_path.read_bytes()) for journal_path in options.logs.rglob("journal.json")]
    archived = next(source for source in journals[0]["sources"] if source["path"] == f"{path}")
    assert archived["sha256"] == hashlib.sha256(original).hexdigest()
    assert archived["images"] == [hashlib.sha256(nes).hexdigest()]


def test_move_retains_7z_without_name_tokens_while_moving_other_sources(
    options: Options, provider: Any, nes: bytes, sevenzip_archive: Callable[..., Path],
) -> None:
    path = sevenzip_archive(options.src / "collection.7z", [("!!!.nes", nes), ("---.nes", nes)])
    original = path.read_bytes()
    (options.src / "a.nes").write_bytes(nes)

    result = run(replace(options, move_roms=True), provider)

    assert result.total == result.removed == 1
    assert path.read_bytes() == original
    assert list(options.src.iterdir()) == [path]


def test_archive_changed_after_staging_is_not_removed(
    options: Options, nes: bytes, sevenzip_archive: Callable[..., Path],
) -> None:
    path = sevenzip_archive(options.src / "game.7z", [("Game.nes", nes)])
    original = path.read_bytes()

    class Mutating:
        def lookup(self, rom: Rom) -> Metadata:
            path.write_bytes(original + b"changed")
            return Metadata()

    with pytest.raises(CatalogueError, match="Source changed"):
        run(replace(options, move_roms=True), Mutating())

    assert path.read_bytes() == original + b"changed"
    assert (options.dst / "catalogue.json").exists()
