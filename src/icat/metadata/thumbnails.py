"""Convert downloaded artwork to the PNG subset understood by IGUI."""

from io import BytesIO

from PIL import Image, UnidentifiedImageError

from ..errors import CatalogueError


def convert(data: bytes) -> bytes:
    try:

        with Image.open(BytesIO(data)) as source:

            if source.width * source.height > 16_000_000:
                raise CatalogueError("Image exceeds 16 megapixels")

            source.thumbnail((640, 480), Image.Resampling.LANCZOS)
            # Composite alpha explicitly; PNG output must be RGB8, not indexed/RGBA.
            rgba = source.convert("RGBA")
            rgb = Image.new("RGB", rgba.size, "black")
            rgb.paste(rgba, mask=rgba.getchannel("A"))
            output = BytesIO()
            rgb.save(output, format="PNG", optimize=True)
            result = output.getvalue()

    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError) as exc:
        raise CatalogueError("Invalid downloaded image") from exc

    if len(result) > 1024 * 1024:
        raise CatalogueError("PNG exceeds IGUI's 1 MiB limit")

    return result
