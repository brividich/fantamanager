"""Checks on what people upload: team images and the files the importers read.

Images (logo, maglie) are served back from the site's own address, so only a
real picture goes through: Pillow must read it as PNG, JPEG, WebP or GIF, and
it is stored under a random name with the extension of what it really is —
never the one the uploader chose, so an ``.html`` or ``.svg`` can't be served
as a page. Import files get a size ceiling, so a crafted spreadsheet or PDF
can't stall the single server process."""
import io
import uuid

from django.core.files.base import ContentFile
from PIL import Image, UnidentifiedImageError

IMAGE_FORMATS = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp", "GIF": "gif"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 4096 * 4096

# Spreadsheets and PDFs read by the importers.
MAX_IMPORT_BYTES = 15 * 1024 * 1024
MAX_IMPORT_ROWS = 20000
MAX_PDF_PAGES = 60

IMAGE_ERROR = "L'immagine deve essere un PNG, JPG, WebP o GIF di massimo 5 MB."
IMPORT_SIZE_ERROR = "Il file è troppo grande (massimo 15 MB)."


class UploadRejected(ValueError):
    """The upload isn't acceptable; ``str()`` is the message for the person."""


def clean_image_bytes(data):
    """``data`` as a ``ContentFile`` with a random, truthful name, or
    ``UploadRejected``."""
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise UploadRejected(IMAGE_ERROR)
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = img.format
            width, height = img.size
            img.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise UploadRejected(IMAGE_ERROR)
    ext = IMAGE_FORMATS.get(fmt)
    if ext is None or width * height > MAX_IMAGE_PIXELS:
        raise UploadRejected(IMAGE_ERROR)
    return ContentFile(data, name=f"{uuid.uuid4().hex}.{ext}")


def clean_image(upload):
    """An uploaded file (``request.FILES[...]``) checked as above."""
    if upload.size is not None and upload.size > MAX_IMAGE_BYTES:
        raise UploadRejected(IMAGE_ERROR)
    return clean_image_bytes(upload.read())


def check_import_file(upload):
    """Size ceiling for a spreadsheet/PDF about to be parsed."""
    if upload is not None and (upload.size or 0) > MAX_IMPORT_BYTES:
        raise UploadRejected(IMPORT_SIZE_ERROR)
    return upload
