"""Draw docs/img/how-it-works.png: a synthetic evening, split per appliance.

All numbers are made up for the illustration (it is not a screenshot of a real
house). Run: python scripts/make_overview.py
"""

from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parents[1] / "docs" / "img" / "how-it-works.png"
W, H, SS = 1500, 760, 2
LEFT, RIGHT, TOP, BOTTOM = 130, 40, 150, 120
MINUTES = 120  # 18:00 - 20:00

BG, INK, MUTED, GRID = (255, 255, 255), (22, 40, 52), (110, 124, 134), (226, 231, 235)
COLOURS = {
    "Untracked": (196, 203, 209),
    "Air conditioner (on/off entity, power learned)": (16, 152, 173),
    "Pressure pump (no entity, inferred)": (255, 183, 3),
    "Shower (no entity, inferred)": (230, 80, 60),
}


def font(px: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    names = ("segoeuib.ttf", "arialbd.ttf") if bold else ("segoeui.ttf", "arial.ttf")
    for name in (*names, "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, px * SS)
        except OSError:
            continue
    return ImageFont.load_default()


def series() -> list[dict[str, float]]:
    rng = random.Random(7)
    rows = []
    for m in range(MINUTES):
        untracked = 380 + rng.gauss(0, 18) + (110 if (m % 25) < 10 else 0)  # fridge
        row = {"Untracked": untracked}
        row["Air conditioner (on/off entity, power learned)"] = 720 if 8 <= m < 95 else 0
        row["Pressure pump (no entity, inferred)"] = 1340 if 41 <= m < 53 else 0
        row["Shower (no entity, inferred)"] = 5500 if 43 <= m < 52 else 0
        rows.append(row)
    return rows


def main() -> None:
    img = Image.new("RGB", (W * SS, H * SS), BG)
    d = ImageDraw.Draw(img)
    x0, x1 = LEFT * SS, (W - RIGHT) * SS
    y0, y1 = TOP * SS, (H - BOTTOM) * SS
    top_w = 8000

    def px(m: float) -> float:
        return x0 + (x1 - x0) * m / MINUTES

    def py(w: float) -> float:
        return y1 - (y1 - y0) * w / top_w

    d.text((LEFT * SS, 34 * SS), "One house meter, split per appliance", font=font(34, True), fill=INK)
    d.text(
        (LEFT * SS, 82 * SS),
        "Synthetic example. The shower and the pump have no sensor or switch in Home Assistant.",
        font=font(20),
        fill=MUTED,
    )

    for w in range(0, top_w + 1, 2000):
        d.line((x0, py(w), x1, py(w)), fill=GRID, width=SS)
        d.text((x0 - 12 * SS, py(w)), f"{w} W", font=font(18), fill=MUTED, anchor="rm")
    for m in range(0, MINUTES + 1, 20):
        d.text((px(m), y1 + 14 * SS), f"{18 + m // 60}:{m % 60:02d}", font=font(18), fill=MUTED, anchor="mt")

    rows = series()
    order = list(COLOURS)
    for m, row in enumerate(rows):
        base = 0.0
        for name in order:
            value = row[name]
            if value > 0:
                d.rectangle((px(m), py(base + value), px(m + 1) + 1, py(base)), fill=COLOURS[name])
            base += value

    for m, label, w in (
        (41, "+1340 W", 1900),
        (43, "+5500 W", 7000),
        (52, "-5500 W", 7000),
    ):
        d.text((px(m) + (-8 if m == 41 else 8) * SS, py(w)), label, font=font(20, True), fill=INK,
               anchor="rm" if m == 41 else "lm")

    lx, ly = LEFT * SS, (H - 70) * SS
    for name, colour in COLOURS.items():
        d.rectangle((lx, ly, lx + 18 * SS, ly + 18 * SS), fill=colour)
        d.text((lx + 28 * SS, ly + 9 * SS), name, font=font(18), fill=INK, anchor="lm")
        lx += (28 + 12 + int(d.textlength(name, font=font(18)) / SS)) * SS
        if lx > (W - 420) * SS:
            lx, ly = LEFT * SS, ly + 30 * SS

    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.resize((W, H), Image.LANCZOS).save(OUT, optimize=True)
    print("wrote", OUT)


if __name__ == "__main__":
    main()
