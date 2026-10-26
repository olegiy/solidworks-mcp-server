"""Validate and sanitize user images in memory before starting a CAD run."""

import base64
import binascii
import io
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

from cadnext.i18n import t

MAX_ATTACHMENTS = 3
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_PIXELS = 16_000_000
MAX_SIDE = 8192
_FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}


class AttachmentError(ValueError):
    pass


class _LimitedBuffer(io.BytesIO):
    def write(self, data):
        if self.tell() + len(data) > MAX_FILE_BYTES:
            raise AttachmentError(t("Изображение после обработки превышает 2 МиБ.",
                                    "The processed image exceeds 2 MiB."))
        return super().write(data)


def normalize_attachments(attachments):
    if not isinstance(attachments, list) or len(attachments) > MAX_ATTACHMENTS:
        raise AttachmentError(t("Можно приложить не более 3 изображений.",
                                "You can attach at most 3 images."))
    decoded = []
    total = 0
    for item in attachments:
        if (not isinstance(item, dict) or set(item) != {"name", "mimeType", "data"}
                or not isinstance(item["name"], str) or not item["name"].strip()
                or len(item["name"]) > 200):
            raise AttachmentError(t("Укажите имя файла изображения длиной от 1 до 200 символов.",
                                    "Provide an image file name from 1 to 200 characters long."))
        mime_type, data = item["mimeType"], item["data"]
        if not isinstance(mime_type, str) or mime_type not in _FORMATS:
            raise AttachmentError(t("Поддерживаются только изображения PNG, JPEG и WebP.",
                                    "Only PNG, JPEG, and WebP images are supported."))
        if not isinstance(data, str) or len(data) > 4 * ((MAX_FILE_BYTES + 2) // 3):
            raise AttachmentError(t("Размер одного файла изображения не должен превышать 2 МиБ.",
                                    "One image file must not exceed 2 MiB."))
        try:
            raw = base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error):
            raise AttachmentError(t("Изображение содержит некорректные данные base64.",
                                    "The image contains invalid base64 data.")) from None
        if not raw or len(raw) > MAX_FILE_BYTES:
            raise AttachmentError(t("Файл изображения пуст или превышает 2 МиБ.",
                                    "The image file is empty or exceeds 2 MiB."))
        total += len(raw)
        if total > MAX_TOTAL_BYTES:
            raise AttachmentError(t("Общий размер файлов изображений не должен превышать 4 МиБ.",
                                    "The total size of image files must not exceed 4 MiB."))
        decoded.append((item, raw))

    normalized = []
    total = 0
    for item, raw in decoded:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw)) as image:
                    if image.format != _FORMATS[item["mimeType"]]:
                        raise AttachmentError(t("Формат изображения не совпадает с заявленным MIME-типом.",
                                        "The image format does not match the declared MIME type."))
                    width, height = image.size
                    if (width > MAX_SIDE or height > MAX_SIDE or width * height > MAX_PIXELS):
                        raise AttachmentError(t("Изображение превышает 16 млн пикселей или сторону 8192 пикселя.",
                                        "The image exceeds 16 million pixels or 8192 pixels per side."))
                    if getattr(image, "n_frames", 1) != 1:
                        raise AttachmentError(t("Анимированные изображения не поддерживаются.",
                                        "Animated images are not supported."))
                    image.verify()
                with Image.open(io.BytesIO(raw)) as image:
                    image.load()
                    with ImageOps.exif_transpose(image) as oriented:
                        mode = "RGBA" if "A" in oriented.getbands() or "transparency" in oriented.info else "RGB"
                        if item["mimeType"] == "image/jpeg":
                            mode = "RGB"
                        with Image.new(mode, oriented.size) as clean:
                            clean.paste(oriented.convert(mode))
                            with _LimitedBuffer() as output:
                                clean.save(output, format=_FORMATS[item["mimeType"]])
                                sanitized = output.getvalue()
        except AttachmentError:
            raise
        except (OSError, ValueError, SyntaxError, UnidentifiedImageError,
                Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise AttachmentError(t("Не удалось прочитать изображение: файл повреждён или небезопасен.",
                                "Could not read the image: the file is corrupted or unsafe.")) from None
        total += len(sanitized)
        if total > MAX_TOTAL_BYTES:
            raise AttachmentError(t("Общий размер изображений после обработки превышает 4 МиБ.",
                                "The total size of processed images exceeds 4 MiB."))
        normalized.append({"name": item["name"], "mimeType": item["mimeType"],
                           "data": base64.b64encode(sanitized).decode("ascii")})
    return normalized
