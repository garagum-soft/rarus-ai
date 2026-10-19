"""Build the Rarus AI icons from the Alem AI globe (owner, 2026-10-19: "the globe icon like alembox, dark yellow with
a very small, 1-2 %, gradient towards orange").

Source: packaging/assets/globe-source.png -- the white Alem AI globe on transparency (alembox-ai/assets-alembox/
globe-white.png). Only its alpha is used: the shape is filled with a dark yellow whose hue slides by ~5 degrees (about
1.4 % of the colour wheel) towards orange from the top-left to the bottom-right corner.

Writes:
  packaging/assets/rarus.ico               the exe, the installer and the shortcuts
  app/rarus_app/rarus-icon.png             the tray icon (256 px)
  engine/ui-web/src/assets/rarus-mark.png  the mark in the page (96 px, shown at 24-48 px)
  engine/ui-web/src/assets/raven-mark.svg  the splash keeps its file name; it now wraps the globe

Run with the build venv: build/venv/Scripts/python.exe tools/dev/make_icons.py
"""

import base64
import io
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "packaging" / "assets" / "globe-source.png"

TOP = (212, 161, 14)       # #D4A10E, dark yellow (hue ~45 deg)
BOTTOM = (214, 150, 15)    # #D6960F, the same yellow ~5 deg nearer orange


def shape() -> Image.Image:
    """The globe's alpha, cropped to the drawing and centred on a square with a small margin."""
    alpha = Image.open(SOURCE).convert("RGBA").getchannel("A")
    box = alpha.getbbox()
    alpha = alpha.crop(box)
    side = int(max(alpha.size) * 1.08)
    square = Image.new("L", (side, side), 0)
    square.paste(alpha, ((side - alpha.width) // 2, (side - alpha.height) // 2))
    return square


def colour(alpha: Image.Image) -> Image.Image:
    n = alpha.width
    ramp = Image.new("RGB", (n, n))
    px = ramp.load()
    for y in range(n):
        for x in range(n):
            t = (x + y) / (2 * (n - 1))
            px[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(TOP, BOTTOM))
    out = ramp.convert("RGBA")
    out.putalpha(alpha)
    return out


def main() -> None:
    big = colour(shape().resize((1024, 1024), Image.LANCZOS))

    ico = ROOT / "packaging" / "assets" / "rarus.ico"
    big.save(ico, sizes=[(s, s) for s in (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)])

    tray = ROOT / "app" / "rarus_app" / "rarus-icon.png"
    big.resize((256, 256), Image.LANCZOS).save(tray, optimize=True)

    mark = ROOT / "engine" / "ui-web" / "src" / "assets" / "rarus-mark.png"
    small = big.resize((96, 96), Image.LANCZOS)
    small.save(mark, optimize=True)

    buf = io.BytesIO()
    big.resize((176, 176), Image.LANCZOS).save(buf, "PNG", optimize=True)
    data = base64.b64encode(buf.getvalue()).decode()
    svg = ROOT / "engine" / "ui-web" / "src" / "assets" / "raven-mark.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="88" height="88" '
        'viewBox="0 0 88 88"><image width="88" height="88" xlink:href="data:image/png;base64,' + data + '"/></svg>\n',
        encoding="utf-8",
    )
    for p in (ico, tray, mark, svg):
        print(p.relative_to(ROOT), p.stat().st_size)


if __name__ == "__main__":
    main()
