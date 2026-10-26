"""Lossless derivatives of accepted drawings; originals remain in local history."""

import base64
import binascii
import io
import warnings

from PIL import Image, ImageChops, UnidentifiedImageError

from cadnext.attachments import MAX_FILE_BYTES, MAX_PIXELS, MAX_SIDE


def optimize_image_url(url: str) -> str:
    if not isinstance(url, str) or not url.startswith("data:image/") or ";base64," not in url:
        return url
    prefix, encoded = url.split(";base64,", 1)
    if prefix not in {"data:image/png", "data:image/jpeg", "data:image/webp"}:
        return url
    if len(encoded) > 4 * ((MAX_FILE_BYTES + 2) // 3):
        return url
    try:
        raw = base64.b64decode(encoded, validate=True)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                width, height = image.size
                if (max(width, height) > MAX_SIDE or width * height > MAX_PIXELS
                        or getattr(image, "n_frames", 1) != 1):
                    return url
                rgba = image.convert("RGBA")
                difference = ImageChops.difference(rgba.convert("RGB"),
                                                   Image.new("RGB", image.size, "white"))
                red, green, blue = difference.split()
                nonwhite = ImageChops.lighter(ImageChops.lighter(red, green), blue).point(
                    lambda value: 255 if value else 0)
                visible = rgba.getchannel("A").point(lambda value: 255 if value else 0)
                bbox = ImageChops.multiply(nonwhite, visible).getbbox()
                if bbox is None:
                    return url
                padding = max(8, round(min(width, height) * .01))
                left, top, right, bottom = bbox
                box = (max(0, left - padding), max(0, top - padding),
                       min(width, right + padding), min(height, bottom + padding))
                if box == (0, 0, width, height):
                    return url
                # PNG preserves the decoded pixels, including faint lines and alpha.
                cropped = image.crop(box)
                if cropped.mode not in {"RGB", "RGBA", "L", "LA", "P"}:
                    cropped = cropped.convert("RGBA")
                output = io.BytesIO()
                cropped.save(output, "PNG")
                candidate = "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")
                return candidate if len(candidate) < len(url) else url
    except (OSError, ValueError, SyntaxError, binascii.Error, UnidentifiedImageError,
            Image.DecompressionBombError, Image.DecompressionBombWarning):
        return url
