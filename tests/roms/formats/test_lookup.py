"""Synthetic representation pairs, without commercial ROMs or file conversion."""

import hashlib
import zlib
from collections.abc import Callable

import pytest

from icat.roms.formats.lookup import LookupNormalizer


@pytest.mark.parametrize("kind, transformation", [
    ("snes", "snes-copier-header"),
    ("smd", "smd-interleaved"),
    ("v64", "n64-byteswap16"),
    ("n64", "n64-byteswap32"),
])
@pytest.mark.parametrize("chunk_size", [1, 7, pytest.param(None, id="whole")])
def test_lookup_is_independent_of_chunk_boundaries(
    lookup_image: Callable[[str], tuple[str, str, bytes, bytes]],
    kind: str, transformation: str, chunk_size: int | None,
) -> None:
    platform, _suffix, canonical, encoded = lookup_image(kind)
    source = bytearray(encoded)
    normalizer = LookupNormalizer(platform=platform, header=encoded[:512], size=len(encoded))
    step = chunk_size or len(source)

    for offset in range(0, len(source), step):
        normalizer.update(memoryview(source)[offset:offset + step])

    expected = (hashlib.sha1(canonical).hexdigest(), len(canonical)), f"{zlib.crc32(canonical):08x}"
    assert normalizer.finish() == expected
    assert normalizer.finish() == expected
    assert normalizer.kind == transformation
    assert source == encoded


@pytest.mark.parametrize("kind, cut", [("snes", 511), ("smd", 512 + 16384 + 3), ("v64", 3), ("n64", 3)])
def test_incomplete_lookup_can_continue(
    lookup_image: Callable[[str], tuple[str, str, bytes, bytes]], kind: str, cut: int,
) -> None:
    platform, _suffix, canonical, encoded = lookup_image(kind)
    normalizer = LookupNormalizer(platform=platform, header=encoded[:512], size=len(encoded))

    normalizer.update(encoded[:cut])

    assert normalizer.finish() is None

    normalizer.update(b"")
    normalizer.update(encoded[cut:])

    assert normalizer.finish() == (
        (hashlib.sha1(canonical).hexdigest(), len(canonical)), f"{zlib.crc32(canonical):08x}",
    )


@pytest.mark.parametrize("kind, payload_size", [("smd", 16384), ("v64", 4), ("n64", 4)])
def test_smallest_supported_images_have_lookup(
    lookup_image: Callable[[str], tuple[str, str, bytes, bytes]], kind: str, payload_size: int,
) -> None:
    platform, _suffix, canonical, encoded = lookup_image(kind)
    header_size = len(encoded) - len(canonical)
    canonical = canonical[:payload_size]
    encoded = encoded[:header_size + payload_size]
    normalizer = LookupNormalizer(platform=platform, header=encoded[:512], size=len(encoded))

    normalizer.update(encoded)

    assert normalizer.finish() == (
        (hashlib.sha1(canonical).hexdigest(), len(canonical)), f"{zlib.crc32(canonical):08x}",
    )


@pytest.mark.parametrize("kind", ["snes", "smd", "v64", "n64"])
@pytest.mark.parametrize("declared_delta, supplied_delta", [
    pytest.param(1, 0, id="wrong-declaration"),
    pytest.param(0, -1, id="truncated"),
    pytest.param(0, 1, id="overrun"),
])
def test_incorrect_stream_lengths_have_no_lookup(
    lookup_image: Callable[[str], tuple[str, str, bytes, bytes]],
    kind: str, declared_delta: int, supplied_delta: int,
) -> None:
    platform, _suffix, _canonical, encoded = lookup_image(kind)
    normalizer = LookupNormalizer(platform=platform, header=encoded[:512], size=len(encoded) + declared_delta)
    supplied = (encoded + b"X")[:len(encoded) + supplied_delta]

    normalizer.update(supplied)

    assert normalizer.finish() is None


@pytest.mark.parametrize("kind", ["snes", "smd", "n64"])
def test_canonical_images_need_no_extra_lookup(
    lookup_image: Callable[[str], tuple[str, str, bytes, bytes]], kind: str,
) -> None:
    platform, _suffix, canonical, _encoded = lookup_image(kind)
    normalizer = LookupNormalizer(platform=platform, header=canonical[:512], size=len(canonical))

    normalizer.update(canonical)

    assert normalizer.finish() is None


@pytest.mark.parametrize("platform, data", [
    pytest.param("SNES", b"", id="empty"),
    pytest.param("SNES", bytes(512), id="snes-header-only"),
    pytest.param("MD", bytes(512), id="smd-header-only"),
    pytest.param("N64", b"\x37\x80", id="short-n64-header"),
    pytest.param("N64", b"\x37\x80\x40\x12X", id="odd-byte-swapped-size"),
    pytest.param("N64", b"\x40\x12\x37\x80XX", id="incomplete-word-size"),
    pytest.param("N64", b"????" + bytes(4), id="unknown-magic"),
    pytest.param("OTHER", b"\x37\x80\x40\x12" + bytes(4), id="unknown-platform"),
])
def test_unsupported_representations_have_no_lookup(platform: str, data: bytes) -> None:
    normalizer = LookupNormalizer(platform=platform, header=data[:512], size=len(data))

    normalizer.update(data)

    assert normalizer.finish() is None


def test_smd_without_cartridge_signature_stays_disabled(
    lookup_image: Callable[[str], tuple[str, str, bytes, bytes]],
) -> None:
    platform, _suffix, _canonical, encoded = lookup_image("smd")
    damaged = encoded[:512] + bytes(16384) + encoded[512 + 16384:]
    normalizer = LookupNormalizer(platform=platform, header=damaged[:512], size=len(damaged))

    normalizer.update(damaged)

    assert normalizer.finish() is None
    assert normalizer.kind is None

    normalizer.update(encoded)

    assert normalizer.finish() is None


@pytest.mark.parametrize("kind", ["v64", "n64"])
def test_n64_stream_must_match_initial_magic(
    lookup_image: Callable[[str], tuple[str, str, bytes, bytes]], kind: str,
) -> None:
    platform, _suffix, _canonical, encoded = lookup_image(kind)
    normalizer = LookupNormalizer(platform=platform, header=encoded[:512], size=len(encoded))

    normalizer.update(b"BAD!" + encoded[4:])

    assert normalizer.finish() is None

