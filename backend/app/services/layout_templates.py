from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

from PIL import Image, ImageDraw, ImageFont


@dataclass
class CoverSpec:
    size: Tuple[int, int] = (1080, 1440)
    margin: int = 64


def _default_font(size: int) -> ImageFont.ImageFont:
    try:
        # macOS
        return ImageFont.truetype("/System/Library/Fonts/PingFang.ttc", size)
    except Exception:
        try:
            return ImageFont.truetype("/System/Library/Fonts/STHeiti Light.ttc", size)
        except Exception:
            return ImageFont.load_default()


def _fit_crop(img: Image.Image, box_w: int, box_h: int) -> Image.Image:
    img = img.convert("RGB")
    iw, ih = img.size
    r = max(box_w / iw, box_h / ih)
    nw, nh = int(iw * r), int(ih * r)
    img = img.resize((nw, nh), Image.Resampling.LANCZOS)
    # center crop
    x1 = max(0, (nw - box_w) // 2)
    y1 = max(0, (nh - box_h) // 2)
    return img.crop((x1, y1, x1 + box_w, y1 + box_h))


def make_cover_a(
    *,
    base_images: List[Image.Image],
    title: str,
    bullets: List[str],
    spec: CoverSpec = CoverSpec(),
) -> Image.Image:
    """Template A: clean cover with large hero image + text panel."""
    W, H = spec.size
    m = spec.margin

    canvas = Image.new("RGB", (W, H), (250, 248, 245))
    draw = ImageDraw.Draw(canvas)

    # hero image region
    hero_h = int(H * 0.62)
    hero = _fit_crop(base_images[0], W - 2 * m, hero_h - m)
    canvas.paste(hero, (m, m))

    # text panel
    panel_y = hero_h
    panel_h = H - panel_y
    draw.rectangle([0, panel_y, W, H], fill=(255, 255, 255))

    title_font = _default_font(56)
    body_font = _default_font(36)

    tx = m
    ty = panel_y + 36
    draw.text((tx, ty), title[:24], fill=(20, 20, 20), font=title_font)
    ty += 78

    bullets = [b.strip("- •\n ") for b in (bullets or []) if b.strip()]
    bullets = bullets[:3]
    for b in bullets:
        draw.text((tx, ty), f"• {b[:28]}", fill=(60, 60, 60), font=body_font)
        ty += 52

    # small brand mark
    small = _default_font(28)
    draw.text((W - m - 220, H - 44), "#洗稿仿写", fill=(120, 120, 120), font=small)

    return canvas


def make_cover_grid(
    *,
    base_images: List[Image.Image],
    title: str,
    spec: CoverSpec = CoverSpec(),
) -> Image.Image:
    """Template: 2x2 grid + title bar."""
    W, H = spec.size
    m = spec.margin

    canvas = Image.new("RGB", (W, H), (248, 248, 250))
    draw = ImageDraw.Draw(canvas)

    bar_h = 180
    draw.rectangle([0, 0, W, bar_h], fill=(255, 255, 255))
    title_font = _default_font(60)
    draw.text((m, 52), title[:20], fill=(10, 10, 10), font=title_font)

    grid_y = bar_h + m
    grid_h = H - grid_y - m
    cell_w = (W - 2 * m - m) // 2
    cell_h = (grid_h - m) // 2

    imgs = (base_images * 4)[:4]
    for idx, img in enumerate(imgs):
        r = idx // 2
        c = idx % 2
        x = m + c * (cell_w + m)
        y = grid_y + r * (cell_h + m)
        tile = _fit_crop(img, cell_w, cell_h)
        canvas.paste(tile, (x, y))

    return canvas
