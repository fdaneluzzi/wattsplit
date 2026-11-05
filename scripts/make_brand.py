"""Regenerate the integration's brand images (icon and logo, light and dark, @2x).

    python scripts/make_brand.py

Writes into custom_components/wattsplit/brand/, which Home Assistant (2026.3+)
serves for the integration without touching the brands repository.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parents[1] / "custom_components" / "wattsplit" / "brand"
SS = 4  # supersampling factor

BG_TOP, BG_BOTTOM = (16, 120, 150), (11, 79, 108)
SLICES = [(255, 255, 255), (255, 212, 59), (105, 219, 124)]  # bolt slices
BOLT = [(300, 44), (132, 288), (236, 288), (200, 468), (382, 214), (278, 214), (332, 44)]
BANDS = [(0, 196), (206, 318), (328, 512)]  # y ranges of the three slices
SHIFT = [0, 16, -2]  # the slices drift apart: the "split"


def _gradient(size: int) -> Image.Image:
    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        t = y / (size - 1)
        row = tuple(round(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM))
        for x in range(size):
            px[x, y] = row
    return img


def icon(size: int) -> Image.Image:
    big = size * SS
    k = big / 512
    base = _gradient(big).convert("RGBA")
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, big - 1, big - 1), radius=int(112 * k), fill=255)
    base.putalpha(mask)

    for (y0, y1), colour, dx in zip(BANDS, SLICES, SHIFT):
        layer = Image.new("L", (big, big), 0)
        ImageDraw.Draw(layer).polygon([(x * k, y * k) for x, y in BOLT], fill=255)
        band = Image.new("L", (big, big), 0)
        ImageDraw.Draw(band).rectangle((0, int(y0 * k), big, int(y1 * k)), fill=255)
        layer = ImageChops.multiply(layer, band)
        layer = ImageChops.offset(layer, int(dx * k), 0)
        base.paste(Image.new("RGBA", (big, big), (*colour, 255)), (0, 0), layer)
    return base.resize((size, size), Image.LANCZOS)


def _font(px: int) -> ImageFont.FreeTypeFont:
    for name in ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    return ImageFont.load_default()


def logo(height: int, dark: bool) -> Image.Image:
    text_colour = (255, 255, 255, 255) if dark else (11, 79, 108, 255)
    font = _font(int(height * 0.52 * SS))
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    left, top, right, bottom = probe.textbbox((0, 0), "WattSplit", font=font)
    text_w, text_h = right - left, bottom - top
    gap = int(height * 0.22 * SS)
    mark = icon(height).resize((height * SS, height * SS), Image.LANCZOS)
    width = height * SS + gap + text_w + int(height * 0.1 * SS)
    canvas = Image.new("RGBA", (width, height * SS), (0, 0, 0, 0))
    canvas.paste(mark, (0, 0), mark)
    ImageDraw.Draw(canvas).text(
        (height * SS + gap - left, (height * SS - text_h) // 2 - top),
        "WattSplit",
        font=font,
        fill=text_colour,
    )
    return canvas.resize((width // SS, height), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for scale, suffix in ((1, ""), (2, "@2x")):
        for prefix in ("", "dark_"):
            icon(256 * scale).save(OUT / f"{prefix}icon{suffix}.png", optimize=True)
            logo(128 * scale, dark=bool(prefix)).save(
                OUT / f"{prefix}logo{suffix}.png", optimize=True
            )
    print("wrote", sorted(p.name for p in OUT.iterdir()))


if __name__ == "__main__":
    main()
