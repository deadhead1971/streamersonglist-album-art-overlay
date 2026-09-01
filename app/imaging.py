"""
Image handling: reflection effect + atomic PNG save, ported from the v2 script.

Two behavioural details are load-bearing and ported deliberately:
- The reflection perspective/fade maths (v2's ``add_reflection``).
- The atomic save with a PermissionError retry loop — OBS holds a read handle on
  the PNG it is displaying, so ``os.replace`` can briefly fail on Windows with
  WinError 32. We retry, then fall back to a direct overwrite.

The v2 constants are now passed in from config: images are stored in the library
at source resolution and resize + reflection happen HERE at output time, so
changing image_size/reflection never requires re-fetching.
"""

import logging
import os
import threading
import time
from io import BytesIO
from pathlib import Path
from typing import Optional

import requests
from PIL import Image

from . import config
from .config import USER_AGENT

log = logging.getLogger("artwork_fetcher")


# ---------------------------------------------------------------------------
# Atomic save (ported verbatim in behaviour from v2)
# ---------------------------------------------------------------------------

def atomic_save_image(img: Image.Image, output_path: str) -> None:
    """
    Save a PIL image as atomically as Windows allows: write a temp file in the
    same directory, then rename it over the target so OBS never reads a
    half-written file.

    OBS keeps an open handle on the displayed PNG, so os.replace() can briefly
    fail with PermissionError (WinError 32). The handle is only held momentarily,
    so we retry, then fall back to a direct overwrite so the image still updates.
    """
    output_path = str(output_path)
    tmp_path = f"{output_path}.tmp"
    img.save(tmp_path, "PNG")

    last_err = None
    for _ in range(10):
        try:
            os.replace(tmp_path, output_path)
            return
        except PermissionError as e:
            last_err = e
            time.sleep(0.05)

    log.warning(
        "Atomic replace blocked after retries (%s); writing directly", last_err
    )
    try:
        img.save(output_path, "PNG")
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Reflection (ported from v2, now driven by a settings dict)
# ---------------------------------------------------------------------------

def add_reflection(img: Image.Image, settings: dict) -> Image.Image:
    """
    Composite the artwork on top of a perspective reflection — wider at the
    bottom, to simulate the album cover standing on a glossy surface.

    ``settings`` keys: height, opacity, gap, perspective, fade_power.
    """
    height_frac = settings.get("height", 0.6)
    opacity = settings.get("opacity", 1.0)
    gap = int(settings.get("gap", 5))
    perspective = settings.get("perspective", 0.55)
    fade_power = settings.get("fade_power", 0.5)

    width, height = img.size
    ref_height = int(height * height_frac)
    if ref_height < 1:
        return img

    flipped = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    reflection = flipped.crop((0, 0, width, ref_height)).convert("RGBA")

    spread_px = int(width * perspective)
    total_width = width + (spread_px * 2)

    perspective_img = Image.new("RGBA", (total_width, ref_height), (0, 0, 0, 0))

    for y in range(ref_height):
        progress = y / max(ref_height - 1, 1)
        row_spread = int(spread_px * progress)
        row_width = width + (row_spread * 2)

        row = reflection.crop((0, y, width, y + 1))
        row_scaled = row.resize((row_width, 1), Image.Resampling.BILINEAR)

        fade = (1.0 - progress) ** fade_power
        row_opacity = opacity * fade

        r, g, b, a = row_scaled.split()
        a = a.point(lambda p: int(p * row_opacity))
        row_scaled = Image.merge("RGBA", (r, g, b, a))

        x_offset = spread_px - row_spread
        perspective_img.paste(row_scaled, (x_offset, y), row_scaled)

    total_height = height + gap + ref_height
    composite = Image.new("RGBA", (total_width, total_height), (0, 0, 0, 0))
    composite.paste(img, (spread_px, 0))
    composite.paste(perspective_img, (0, height + gap), perspective_img)

    return composite


# ---------------------------------------------------------------------------
# Download + render
# ---------------------------------------------------------------------------

def download_image_bytes(url: str, timeout: int = 15) -> bytes:
    """Download raw image bytes. Raises requests.RequestException on failure."""
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def render_output(source_img: Image.Image, config: dict) -> Image.Image:
    """
    Turn a source-resolution library image into the OBS output image: convert to
    RGBA, resize to the configured square size, apply reflection if enabled.
    """
    size = int(config.get("image_size", 640))
    reflection = config.get("reflection", {})

    img = source_img.convert("RGBA")
    if img.size != (size, size):
        img = img.resize((size, size), Image.Resampling.LANCZOS)

    if reflection.get("enabled", True):
        img = add_reflection(img, reflection)

    return img


def render_output_from_path(path: str, config: dict) -> Image.Image:
    with Image.open(path) as im:
        im.load()
        return render_output(im, config)


def render_output_from_bytes(data: bytes, config: dict) -> Image.Image:
    with Image.open(BytesIO(data)) as im:
        im.load()
        return render_output(im, config)


# ---------------------------------------------------------------------------
# Art wall thumbnails
# ---------------------------------------------------------------------------

# The wall's whole performance story. Pointing a browser source at 188
# full-resolution covers decodes to ~282MB of bitmap while the machine is also
# encoding video; at 512px, the 18 visible tiles plus a small preload buffer
# come to ~23MB, and that figure stays flat however big the library grows.
#
# Three buckets, snapped server-side so the cache cannot sprawl. WebP rather
# than PNG because album art has no alpha to preserve: ~60KB a tile against
# ~400KB, putting a whole 128-tile cache under 8MB.
THUMB_SIZES = (256, 512, 1024)


def snap_thumb_size(requested: int) -> int:
    """Round a requested tile width up to the nearest cached bucket."""
    for size in THUMB_SIZES:
        if requested <= size:
            return size
    return THUMB_SIZES[-1]


def make_thumbnail(src_path, size: int) -> bytes:
    """
    Square WebP thumbnail of a library image, centre-cropped to fill.

    Cropping rather than letterboxing: a handful of library images are not
    square (1280x720 grabs, 918x1000 scans) and a gapless mosaic with one
    letterboxed tile in it reads as a broken tile.
    """
    with Image.open(src_path) as im:
        im.load()
        img = im.convert("RGB")

    width, height = img.size
    side = min(width, height)
    if (width, height) != (side, side):
        left = (width - side) // 2
        top = (height - side) // 2
        img = img.crop((left, top, left + side, top + side))
    if img.size != (size, size):
        img = img.resize((size, size), Image.Resampling.LANCZOS)

    buf = BytesIO()
    img.save(buf, "WEBP", quality=82, method=4)
    return buf.getvalue()


def thumbnail_path(digest: str, size: int) -> Path:
    return config.THUMBS_DIR / f"{digest}_{size}.webp"


def ensure_thumbnail(src_path, digest: str, size: int) -> Optional[Path]:
    """
    Path to the cached thumbnail for this image, generating it on first ask.
    None if the source image cannot be read.

    Generation is lazy by design: the first load of a wall assembles over a few
    seconds as tiles finish, which is the intended reveal rather than a stall.
    Browsers cap themselves at roughly six connections per host, so the burst
    self-throttles and needs no queue here.
    """
    dest = thumbnail_path(digest, size)
    if dest.exists():
        return dest

    try:
        data = make_thumbnail(src_path, size)
    except (OSError, ValueError) as e:
        log.info("Thumbnail failed for %s: %s", src_path, e)
        return None

    dest.parent.mkdir(parents=True, exist_ok=True)
    # Several connections can ask for the same tile at once. Each writes its own
    # temp file and renames it into place, so no reader ever sees a partial one
    # and whoever loses the race simply discards their copy.
    tmp = dest.with_name(f"{dest.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, dest)
    except OSError as e:
        log.info("Thumbnail cache write failed for %s: %s", dest.name, e)
        try:
            tmp.unlink()
        except OSError:
            pass

    return dest if dest.exists() else None


def to_png_bytes(data_or_img) -> bytes:
    """
    Normalise arbitrary image input (bytes of jpg/png/webp, or a PIL image) to
    PNG bytes at source resolution — used when storing into the library.
    """
    if isinstance(data_or_img, (bytes, bytearray)):
        with Image.open(BytesIO(bytes(data_or_img))) as im:
            im.load()
            img = im.convert("RGBA")
    else:
        img = data_or_img.convert("RGBA")
    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
