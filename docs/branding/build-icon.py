#!/usr/bin/env python3
"""Regenerate Contents/Resources/icon.png from the two sources in this folder.

    python3 docs/branding/build-icon.py

Composites the official Matter symbol (cropped out of the upstream lockup,
recoloured white) inside the Indigo house frame, then downsamples to the 256x256
the Plugin Store wants. Rendering needs Chrome (headless) and Pillow.

Kept as a script rather than a hand-edited PNG so the icon can be adjusted —
symbol size, placement, house colours — without redrawing anything.
"""
import os
import subprocess
import sys

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
OUT = os.path.join(REPO, "indigo-matter.indigoPlugin", "Contents", "Resources", "icon.png")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# Design units are the icon's 256x256 viewBox; RENDER is the supersample factor.
RENDER = 1024
SCALE = RENDER / 256.0
SYMBOL_WIDTH = 86   # design units — 100 crowds the walls, 74 looks lost
# y136 centres it in the house: the frame spans y44-204, and lower values
# (152 was the first cut) leave an obvious gap under the gable, while 130
# starts crowding the roof apex.
SYMBOL_CX, SYMBOL_CY = 128, 136


def _shot(svg, png, w, h):
    subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
         "--force-device-scale-factor=1", f"--window-size={w},{h}",
         "--default-background-color=00000000", f"--screenshot={png}", f"file://{svg}"],
        check=True, capture_output=True,
    )


def _white_symbol(lockup_png):
    """Crop the symbol out of the upstream lockup and recolour it white.

    The upstream file is one path holding both the wordmark and the symbol, so
    the symbol cannot be separated by path index — it is isolated positionally
    (it sits left of the wordmark) and then alpha-keyed off its darkness.
    """
    left = Image.open(lockup_png).convert("RGBA").crop((0, 0, 520, 344))
    px = left.load()
    xs, ys = [], []
    for y in range(left.height):
        for x in range(left.width):
            r, g, b, a = px[x, y]
            if a > 40 and r < 120 and g < 120 and b < 120:
                xs.append(x)
                ys.append(y)
    if not xs:
        sys.exit("could not find the symbol in the upstream lockup")
    sym = left.crop((min(xs), min(ys), max(xs) + 1, max(ys) + 1))
    out = Image.new("RGBA", sym.size, (255, 255, 255, 0))
    s, o = sym.load(), out.load()
    for y in range(sym.height):
        for x in range(sym.width):
            r, g, b, a = s[x, y]
            if a:
                alpha = int(a * (1.0 - (r + g + b) / 3 / 255.0))
                if alpha:
                    o[x, y] = (255, 255, 255, alpha)
    return out


def main():
    tmp = os.path.join(HERE, ".build")
    os.makedirs(tmp, exist_ok=True)
    house_png = os.path.join(tmp, "house.png")
    lock_html = os.path.join(tmp, "lockup.html")
    lock_png = os.path.join(tmp, "lockup.png")

    _shot(os.path.join(HERE, "icon-house.svg"), house_png, RENDER, RENDER)
    with open(lock_html, "w") as fh:
        fh.write('<body style="margin:0;background:#fff">'
                 '<div style="width:1600px;height:344px;display:flex;'
                 'align-items:center;justify-content:center">'
                 f'<img src="{os.path.join(HERE, "matter-logo-upstream.svg")}" style="width:1560px">'
                 "</div></body>")
    _shot(lock_html, lock_png, 1600, 344)

    house = Image.open(house_png).convert("RGBA")
    sym = _white_symbol(lock_png)
    w = int(SYMBOL_WIDTH * SCALE)
    h = int(sym.height * (w / sym.width))
    house.alpha_composite(sym.resize((w, h), Image.LANCZOS),
                          (int(SYMBOL_CX * SCALE - w / 2), int(SYMBOL_CY * SCALE - h / 2)))
    house.resize((256, 256), Image.LANCZOS).save(OUT)
    print(f"wrote {OUT} (256x256)")


if __name__ == "__main__":
    main()
