#!/usr/bin/env python3
"""Draws a simple 1200×800 cover: a two-colour gradient with the set title.

Usage: python3 tools/make_cover.py images/fruits.png "Fruits" "#FF7A59" "#FFC15E"
Needs Pillow (pip install pillow).
"""

import sys

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1200, 800
FONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"


def hex_colour(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def main(path: str, title: str, start: str, end: str) -> None:
    top, bottom = hex_colour(start), hex_colour(end)
    image = Image.new("RGB", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(image)
    for y in range(HEIGHT):
        t = y / (HEIGHT - 1)
        draw.line([(0, y), (WIDTH, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)))

    size = 140
    font = ImageFont.truetype(FONT, size)
    while draw.textlength(title, font=font) > WIDTH - 160 and size > 40:
        size -= 6
        font = ImageFont.truetype(FONT, size)
    draw.text((WIDTH / 2, HEIGHT / 2), title, font=font, fill="white", anchor="mm")
    image.save(path, optimize=True)


if __name__ == "__main__":
    if len(sys.argv) != 5:
        sys.exit(__doc__)
    main(*sys.argv[1:])
