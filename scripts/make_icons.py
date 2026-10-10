"""Generate the Navigator tile icons and Composer device images.

Style matches the stock Control4 "Experience Button" icons (and DirectorLink · Samsung
Refrigerator's tiles): a gray disc with a colored glow ring, and a front-loading washer.

States of the "Washer" tile (the uibutton proxy picks the icon by state id):
  off       gray ring, dark door
  ready     soft blue ring, dark door           (switched on, not running)
  running   blue ring, water in the drum
  paused    amber ring, water in the drum, pause badge
  finished  green ring, check badge
  error     red ring, "!" badge
  pending   white ring, dots                    (refreshing, or a command being confirmed)
  unknown   gray ring, faded washer             (not set up, or no status yet)

Composer images: icons/device_sm.png / device_lg.png (the driver) and
icons/washer/device_sm.png / device_lg.png (the proxy).

Usage:  python scripts/make_icons.py
"""
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
ICON_DIR = ROOT / "src" / "www" / "icons"
SIZES = (70, 90, 300, 512, 1024)
STATES = ("off", "ready", "running", "paused", "finished", "error", "pending", "unknown")
SS = 4          # supersampling factor
BASE = 300      # design grid

RING = {"off": (95, 95, 95), "ready": (120, 170, 220), "running": (30, 140, 255), "paused": (255, 165, 0),
        "finished": (60, 200, 110), "error": (230, 40, 40), "pending": (235, 235, 235), "unknown": (80, 80, 80)}
GLOW = ("running", "paused", "finished", "error", "pending")
WATER = (40, 150, 255, 255)


def S(v, k):
    return int(round(v * k))


def gradient_disc(W, box, top, bottom):
    grad = Image.new("RGBA", (W, W))
    gd = ImageDraw.Draw(grad)
    for y in range(W):
        t = min(max((y - box[1]) / max(1, box[3] - box[1]), 0), 1)
        gd.line([(0, y), (W, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,))
    mask = Image.new("L", (W, W), 0)
    ImageDraw.Draw(mask).ellipse(box, fill=255)
    out = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)
    return out


def glyph_washer(W, k, water):
    layer = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    body, line, rim = (246, 246, 246, 255), (150, 150, 155, 255), (200, 200, 206, 255)
    lw = max(1, S(4, k))
    d.rounded_rectangle([S(90, k), S(58, k), S(210, k), S(242, k)], radius=S(14, k), fill=body)
    d.line([(S(90, k), S(98, k)), (S(210, k), S(98, k))], fill=line, width=lw)            # control panel
    d.rounded_rectangle([S(104, k), S(72, k), S(146, k), S(86, k)], radius=S(3, k), fill=(70, 74, 80, 255))
    d.ellipse([S(176, k), S(70, k), S(194, k), S(88, k)], fill=rim, outline=line, width=max(1, S(2, k)))
    cx, cy = 150, 168
    d.ellipse([S(cx - 52, k), S(cy - 52, k), S(cx + 52, k), S(cy + 52, k)], fill=rim)        # door rim
    d.ellipse([S(cx - 41, k), S(cy - 41, k), S(cx + 41, k), S(cy + 41, k)], fill=(70, 74, 80, 255))
    if water:
        # water filling the lower part of the drum, with a wave on top
        mask = Image.new("L", (W, W), 0)
        md = ImageDraw.Draw(mask)
        md.ellipse([S(cx - 41, k), S(cy - 41, k), S(cx + 41, k), S(cy + 41, k)], fill=255)
        wave = Image.new("L", (W, W), 0)
        wd = ImageDraw.Draw(wave)
        pts = [(S(cx - 45, k), S(cy + 45, k))]
        for i in range(0, 91, 3):
            x = cx - 45 + i
            pts.append((S(x, k), S(cy - 2 + 6 * math.sin(i / 90 * 2 * math.pi * 1.5), k)))
        pts.append((S(cx + 45, k), S(cy + 45, k)))
        wd.polygon(pts, fill=255)
        water_layer = Image.new("RGBA", (W, W), WATER)
        both = Image.new("L", (W, W), 0)
        both.paste(wave, (0, 0), mask)
        layer.paste(water_layer, (0, 0), both)
        d = ImageDraw.Draw(layer)
        for bx, by, r in ((cx - 14, cy + 16, 5), (cx + 10, cy + 24, 4), (cx + 20, cy + 8, 3)):
            d.ellipse([S(bx - r, k), S(by - r, k), S(bx + r, k), S(by + r, k)], fill=(225, 240, 255, 230))
    # glass highlight
    d.arc([S(cx - 32, k), S(cy - 32, k), S(cx + 32, k), S(cy + 32, k)], start=200, end=250,
          fill=(255, 255, 255, 170), width=max(1, S(5, k)))
    return layer


def badge(d, k, color):
    d.ellipse([S(196, k), S(196, k), S(262, k), S(262, k)], fill=color + (255,), outline=(255, 255, 255, 255),
              width=S(5, k))


def make_icon(state, size):
    W = size * SS
    k = W / BASE
    img = Image.new("RGBA", (W, W), (0, 0, 0, 0))

    shadow = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse([S(22, k), S(28, k), S(282, k), S(288, k)], fill=(0, 0, 0, 110))
    img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(S(6, k))))

    ringimg = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    ImageDraw.Draw(ringimg).ellipse([S(14, k), S(14, k), S(286, k), S(286, k)], fill=RING[state] + (255,))
    if state in GLOW:
        img = Image.alpha_composite(img, ringimg.filter(ImageFilter.GaussianBlur(S(5, k))))
    img = Image.alpha_composite(img, ringimg)
    img = Image.alpha_composite(img, gradient_disc(W, [S(36, k), S(36, k), S(264, k), S(264, k)],
                                                   (165, 165, 165), (95, 95, 95)))

    glyph = glyph_washer(W, k, state in ("running", "paused"))
    if state == "unknown":
        r, g, b, a = glyph.split()
        glyph = Image.merge("RGBA", (r, g, b, a.point(lambda v: v * 35 // 100)))
    img = Image.alpha_composite(img, glyph)

    d = ImageDraw.Draw(img)
    white = (255, 255, 255, 255)
    if state == "error":
        badge(d, k, (230, 40, 40))
        d.rounded_rectangle([S(224, k), S(209, k), S(234, k), S(236, k)], radius=S(4, k), fill=white)
        d.ellipse([S(223, k), S(240, k), S(235, k), S(252, k)], fill=white)
    elif state == "paused":
        badge(d, k, (255, 150, 0))
        for x in (214, 236):
            d.rounded_rectangle([S(x - 1, k), S(212, k), S(x + 9, k), S(246, k)], radius=S(3, k), fill=white)
    elif state == "finished":
        badge(d, k, (40, 170, 80))
        d.line([(S(212, k), S(230, k)), (S(225, k), S(243, k)), (S(247, k), S(214, k))], fill=white,
               width=S(8, k), joint="curve")
    elif state == "pending":
        for i, x in enumerate((128, 150, 172)):
            d.ellipse([S(x - 7, k), S(250, k), S(x + 7, k), S(264, k)], fill=(255, 255, 255, 255 - i * 60))

    return img.resize((size, size), Image.LANCZOS)


def main():
    folder = ICON_DIR / "washer"
    folder.mkdir(parents=True, exist_ok=True)
    for state in STATES:
        for size in SIZES:
            make_icon(state, size).save(folder / f"{state}_{size}.png", optimize=True)
    for target in (folder, ICON_DIR):
        make_icon("running", 32).save(target / "device_lg.png", optimize=True)
        make_icon("running", 16).save(target / "device_sm.png", optimize=True)
    print(f"Icons written to {ICON_DIR}")


if __name__ == "__main__":
    main()
