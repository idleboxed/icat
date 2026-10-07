

from io import BytesIO

import pytest
from PIL import Image

from icat.errors import CatalogueError
from icat.metadata.thumbnails import convert


@pytest.mark.parametrize(
    ("mode", "size", "expected"),
    [
        ("RGB", (1920, 1080), (640, 360)),
        ("RGBA", (640, 480), (640, 480)),
        ("P", (256, 240), (256, 240)),
        ("L", (160, 144), (160, 144)),
    ],
)
def test_thumbnails_fit_decoder_limits_without_upscaling(
    mode: str, size: tuple[int, int], expected: tuple[int, int],
) -> None:
    stream = BytesIO()
    Image.new(mode, size).save(stream, "PNG")

    data = convert(stream.getvalue())

    with Image.open(BytesIO(data)) as image:
        assert image.mode == "RGB" and image.size == expected

    assert data[24:26] == bytes([8, 2])  # PNG RGB, 8-bit.
    assert len(data) <= 1024 * 1024

def test_bad_image_is_rejected() -> None:

    with pytest.raises(CatalogueError, match="Invalid downloaded image"):
        convert(b"not an image")
