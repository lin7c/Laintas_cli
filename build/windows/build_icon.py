#!/usr/bin/env python3
"""Regenerate build/windows/icon.ico from build/windows/icon.svg.

The .ico is committed rather than built in CI: the Windows runner has no SVG
rasteriser, and the icon is the one build input that changes about once a
year. Run this by hand when icon.svg changes, and commit the result.

Requires `rsvg-convert` (librsvg2-bin) and Pillow.

Each size is rendered from the SVG at its own resolution rather than
downscaled from one big bitmap, so the rounded corners stay clean at 16px.
Sizes below 256 are written as 32-bit DIB entries, which is what Explorer has
read since forever; 256 goes in as PNG, the only form Windows accepts there.
"""

import os
import struct
import subprocess
import sys
import tempfile

from PIL import Image

SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]
HERE = os.path.dirname(os.path.abspath(__file__))
SVG = os.path.join(HERE, 'icon.svg')
ICO = os.path.join(HERE, 'icon.ico')


def render(tmp: str) -> list[tuple[int, bytes]]:
    entries = []
    for size in SIZES:
        png = os.path.join(tmp, f'{size}.png')
        subprocess.run(['rsvg-convert', '-w', str(size), '-h', str(size),
                        SVG, '-o', png], check=True)
        if size == 256:
            with open(png, 'rb') as handle:
                entries.append((size, handle.read()))
            continue
        image = Image.open(png).convert('RGBA')
        pixels = image.load()
        # BITMAPINFOHEADER, height doubled to account for the AND mask.
        header = struct.pack('<IiiHHIIiiII', 40, size, size * 2, 1, 32,
                             0, 0, 0, 0, 0, 0)
        colour = bytearray()
        for y in range(size - 1, -1, -1):       # DIB rows run bottom-up
            for x in range(size):
                r, g, b, a = pixels[x, y]
                colour += bytes((b, g, r, a))
        row = ((size + 31) // 32) * 4           # mask rows pad to 4 bytes
        mask = bytes(row * size)                # all zero: alpha carries it
        entries.append((size, header + bytes(colour) + mask))
    return entries


def main() -> int:
    if not os.path.isfile(SVG):
        print(f'no {SVG}', file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        entries = render(tmp)
    out = bytearray(struct.pack('<HHH', 0, 1, len(entries)))
    offset = 6 + 16 * len(entries)
    for size, data in entries:
        # 256 is written as 0 in the directory: the field is a single byte.
        out += struct.pack('<BBBBHHII', size % 256, size % 256, 0, 0,
                           1, 32, len(data), offset)
        offset += len(data)
    for _, data in entries:
        out += data
    with open(ICO, 'wb') as handle:
        handle.write(out)
    print(f'{ICO}: {len(entries)} sizes, {len(out)} bytes')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
