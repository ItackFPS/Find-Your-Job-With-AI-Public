"""Draw the application icon (packaging/windows/FindYourJob.ico) with the standard library only.

A white magnifying glass over a briefcase on a rounded blue square. Deterministic: running it
again rewrites the same bytes, so the committed icon can be checked against this script
(tests/test_windows_package.py).

    python packaging/windows/make_icon.py
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

ICON_PATH = Path(__file__).with_name("FindYourJob.ico")
SIZES = (16, 24, 32, 48, 64, 128, 256)
SUPERSAMPLE = 4

BLUE = (37, 99, 235)
WHITE = (255, 255, 255)
AMBER = (251, 191, 36)


def _rounded_rect(x: float, y: float, x0: float, y0: float, x1: float, y1: float, r: float) -> bool:
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def _colour_at(x: float, y: float) -> tuple[int, int, int, int]:
    """Colour of the 1x1 design at (x, y) in [0, 1]^2 (RGBA, alpha 0 = transparent)."""
    if not _rounded_rect(x, y, 0.02, 0.02, 0.98, 0.98, 0.2):
        return (0, 0, 0, 0)
    # magnifying glass: ring centred at (0.44, 0.42) and a handle towards the lower right
    d = math.hypot(x - 0.44, y - 0.42)
    if 0.2 <= d <= 0.27:
        return (*WHITE, 255)
    hx, hy = x - 0.44, y - 0.42
    along = (hx + hy) / math.sqrt(2)
    across = abs(hx - hy) / math.sqrt(2)
    if 0.25 <= along <= 0.5 and across <= 0.055:
        return (*WHITE, 255)
    # briefcase inside the lens
    if _rounded_rect(x, y, 0.31, 0.39, 0.57, 0.53, 0.03):
        return (*AMBER, 255)
    if _rounded_rect(x, y, 0.39, 0.33, 0.49, 0.4, 0.02) and not _rounded_rect(x, y, 0.415, 0.355, 0.465, 0.4, 0.01):
        return (*AMBER, 255)
    return (*BLUE, 255)


def render(size: int) -> bytes:
    """RGBA rows, top to bottom, antialiased by supersampling."""
    rows = bytearray()
    n = SUPERSAMPLE
    for py in range(size):
        rows.append(0)  # PNG filter type: none
        for px in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(n):
                for sx in range(n):
                    r, g, b, a = _colour_at((px + (sx + 0.5) / n) / size, (py + (sy + 0.5) / n) / size)
                    acc[0] += r * a
                    acc[1] += g * a
                    acc[2] += b * a
                    acc[3] += a
            alpha = acc[3] // (n * n)
            if acc[3]:
                rows += bytes((acc[0] // acc[3], acc[1] // acc[3], acc[2] // acc[3], alpha))
            else:
                rows += b"\0\0\0\0"
    return bytes(rows)


def png(size: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(render(size), 9)) + chunk(b"IEND", b""))


def ico() -> bytes:
    images = [png(s) for s in SIZES]
    out = bytearray(struct.pack("<HHH", 0, 1, len(images)))
    offset = 6 + 16 * len(images)
    for size, data in zip(SIZES, images):
        dim = 0 if size >= 256 else size
        out += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for data in images:
        out += data
    return bytes(out)


if __name__ == "__main__":
    ICON_PATH.write_bytes(ico())
    print(f"wrote {ICON_PATH}")
