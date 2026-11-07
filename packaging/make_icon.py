"""
Draw packaging/icon.ico: a vinyl record with the dashboard's accent-blue label.

Run once when the design changes (`python packaging/make_icon.py`); the .ico is
committed. Drawn large and downsampled per size, so small sizes stay smooth.
The same file is the exe's icon, the tray icon and the dashboard favicon.
"""
from pathlib import Path

from PIL import Image, ImageDraw

ACCENT = (91, 140, 255, 255)   # --accent (#5b8cff) in app/static/style.css
DISC = (24, 24, 28, 255)
GROOVE = (58, 58, 66, 255)
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
BIG = 1024


def draw() -> Image.Image:
    img = Image.new("RGBA", (BIG, BIG), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = 16
    d.ellipse((m, m, BIG - m, BIG - m), fill=DISC)
    for r in (440, 395, 350):  # grooves
        d.ellipse((BIG / 2 - r, BIG / 2 - r, BIG / 2 + r, BIG / 2 + r),
                  outline=GROOVE, width=14)
    label = 230
    d.ellipse((BIG / 2 - label, BIG / 2 - label, BIG / 2 + label, BIG / 2 + label),
              fill=ACCENT)
    hole = 52
    d.ellipse((BIG / 2 - hole, BIG / 2 - hole, BIG / 2 + hole, BIG / 2 + hole),
              fill=(0, 0, 0, 0))
    return img


if __name__ == "__main__":
    out = Path(__file__).with_name("icon.ico")
    big = draw()
    big.resize((256, 256), Image.LANCZOS).save(
        out, format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"wrote {out}")
