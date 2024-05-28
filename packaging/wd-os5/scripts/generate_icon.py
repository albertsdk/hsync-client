#!/usr/bin/env python3
"""Generate the HSYNC app icon (PNG) for the WD My Cloud OS5 package.

Reproduces the Android launcher design from
``hsync-android/app/src/main/res/drawable/ic_launcher_*.xml``:

* teal background (``md_primary`` = #006A6A)
* dark wave overlay at the bottom (``md_on_background`` @ 30 % alpha)
* white block ``H`` letter
* two opposing sync arrows

Run:
    python3 generate_icon.py [--size 160] [--out Apkg/hsync.png]

The default size (160 px) matches what the WD admin dashboard uses for the
``Apps`` grid; PIL keeps the geometry clean at any power-of-two scale.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover
    print("Pillow is required: pip install Pillow", file=sys.stderr)
    sys.exit(1)

# Palette mirrors hsync-android/app/src/main/res/values/colors.xml
PRIMARY = (0x00, 0x6A, 0x6A, 0xFF)  # md_primary teal
OVERLAY = (0x16, 0x1D, 0x1D, int(0xFF * 0.3))  # md_on_background @ 30 %
WHITE = (0xFF, 0xFF, 0xFF, 0xFF)
WHITE_DIM = (0xFF, 0xFF, 0xFF, int(0xFF * 0.7))


def _scale(coords: tuple[float, ...], k: float) -> tuple[float, ...]:
    return tuple(c * k for c in coords)


def render(size: int) -> Image.Image:
    """Compose the icon at the requested square size (pixels)."""
    img = Image.new("RGBA", (size, size), PRIMARY)
    overlay = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw_o = ImageDraw.Draw(overlay)

    # Scale factor against the Android 108-unit viewport so the geometry
    # below can read 1:1 against ic_launcher_*.xml.
    k = size / 108.0

    # Wave overlay: quadratic bezier approximated with a polygon at the
    # bottom half (M0,54 Q54,80 108,54 v54 H0z).
    pts: list[tuple[float, float]] = []
    steps = 32
    for i in range(steps + 1):
        t = i / steps
        x = 108 * t
        # Quadratic bezier through (0,54), (54,80), (108,54)
        y = (1 - t) ** 2 * 54 + 2 * (1 - t) * t * 80 + t**2 * 54
        pts.append(_scale((x, y), k))  # type: ignore[arg-type]
    pts += [_scale((108, 108), k), _scale((0, 108), k)]  # type: ignore[arg-type]
    draw_o.polygon(pts, fill=OVERLAY)

    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)

    # H — left bar, right bar, horizontal connector.
    draw.rectangle(_scale((34, 26, 46, 82), k), fill=WHITE)  # type: ignore[arg-type]
    draw.rectangle(_scale((62, 26, 74, 82), k), fill=WHITE)  # type: ignore[arg-type]
    draw.rectangle(_scale((46, 46, 62, 62), k), fill=WHITE)  # type: ignore[arg-type]

    # Sync arrows — top-right pointing right, bottom-left pointing left.
    # Original path: M74,42 l6,6 -6,6 v-4 h-8 v-4 h8 z
    top = [(74, 42), (80, 48), (74, 54), (74, 50), (66, 50), (66, 46), (74, 46)]
    bottom = [(34, 66), (28, 60), (34, 54), (34, 58), (42, 58), (42, 62), (34, 62)]
    draw.polygon([_scale(p, k) for p in top], fill=WHITE_DIM)  # type: ignore[arg-type]
    draw.polygon([_scale(p, k) for p in bottom], fill=WHITE_DIM)  # type: ignore[arg-type]

    return img


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the HSYNC OS5 app icon.")
    parser.add_argument("--size", type=int, default=160, help="square edge length in pixels")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "Apkg" / "hsync.png",
        help="output PNG path",
    )
    args = parser.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    render(args.size).save(args.out, format="PNG", optimize=True)
    print(f"wrote {args.out} ({args.size}x{args.size})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
