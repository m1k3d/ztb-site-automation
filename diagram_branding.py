"""Bounded local logo uploads, normalized to metadata-free PNGs for exports."""
import base64
import binascii
import io
import warnings
from functools import lru_cache

MAX_UPLOAD = 2 * 1024 * 1024


@lru_cache(maxsize=16)
def _image(content):
    from PIL import Image, ImageOps, UnidentifiedImageError
    if len(content) > (MAX_UPLOAD + 2) // 3 * 4:
        raise ValueError('Choose a JPG or PNG logo up to 2 MB.')
    try:
        raw = base64.b64decode(content, validate=True)
        if not raw or len(raw) > MAX_UPLOAD:
            raise ValueError('Choose a JPG or PNG logo up to 2 MB.')
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw), formats=['JPEG', 'PNG']) as picture:
                if max(picture.size) > 4096 or picture.width * picture.height > 12_000_000:
                    raise ValueError('Choose a logo no larger than 4096 pixels per side and 12 megapixels.')
                if getattr(picture, 'n_frames', 1) != 1:
                    raise ValueError('Choose a still JPG or PNG logo.')
                picture.load()
                picture = ImageOps.exif_transpose(picture).convert('RGBA')
                picture.thumbnail((600, 200), Image.Resampling.LANCZOS)
                # Rebuild pixels to discard EXIF, comments, profiles, and other metadata.
                clean = Image.frombytes('RGBA', picture.size, picture.tobytes())
                while True:
                    output = io.BytesIO(); clean.save(output, format='PNG', optimize=True)
                    if output.tell() <= 72 * 1024:break
                    clean=clean.resize((max(1,round(clean.width*.8)),max(1,round(clean.height*.8))),Image.Resampling.LANCZOS)
                return base64.b64encode(output.getvalue()).decode(), clean.width, clean.height
    except (binascii.Error, UnidentifiedImageError, OSError, SyntaxError,
            Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError('This file could not be read as a JPG or PNG logo.') from None


def logo(value):
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get('content'), str):
        raise ValueError('Choose a JPG or PNG logo, or use the Zscaler logo.')
    content, width, height = _image(value['content'])
    return dict(content=content, width=width, height=height)
