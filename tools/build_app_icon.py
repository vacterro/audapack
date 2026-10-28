#!/usr/bin/env python
"""T-176 dev tool: regenerate resources/app_icon.ico as a real multi-size ICO.

Development-time only -- AUDAPACK runtime never imports Pillow. Embedded
sizes: 16, 32, 48, 256, sourced from the canonical app_icon.png (16/32 come
from the shipped exact-size PNGs) with NEAREST scaling (pixel art, no
antialiasing). Each frame is stored PNG-compressed inside the ICO container.
"""

import io
import struct
from pathlib import Path

from PIL import Image

RES = Path(__file__).resolve().parents[1] / "resources"


def _png_bytes(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def main() -> None:
    src = Image.open(RES / "app_icon.png").convert("RGBA")
    frames: list[tuple[int, int, Image.Image]] = []
    for size, name in ((16, "app_icon_16.png"), (32, "app_icon_32.png")):
        exact = Image.open(RES / name).convert("RGBA")
        im = exact if exact.size == (size, size) else src.resize((size, size), Image.NEAREST)
        frames.append((size, size, im))
    frames.append((48, 48, src.resize((48, 48), Image.NEAREST)))
    frames.append((256, 256, src if src.size == (256, 256) else src.resize((256, 256), Image.NEAREST)))

    blobs = [(w, h, _png_bytes(im)) for w, h, im in frames]
    header = struct.pack("<HHH", 0, 1, len(blobs))
    offset = 6 + 16 * len(blobs)
    entries = []
    body = []
    for w, h, blob in blobs:
        entries.append(
            struct.pack(
                "<BBBBHHII",
                0 if w == 256 else w,
                0 if h == 256 else h,
                0,  # bColorCount
                0,  # bReserved
                1,  # wPlanes
                32,  # wBitCount
                len(blob),
                offset,
            )
        )
        body.append(blob)
        offset += len(blob)
    (RES / "app_icon.ico").write_bytes(header + b"".join(entries) + b"".join(body))
    print("wrote", RES / "app_icon.ico", "entries:", [w for w, _, _ in frames])


if __name__ == "__main__":
    main()
