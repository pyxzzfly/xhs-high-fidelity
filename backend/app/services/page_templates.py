from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

from PIL import Image, ImageDraw, ImageFont


@dataclass
class PageSpec:
    size: Tuple[int, int] = (1080, 1440)
    margin: int = 64


def _default_font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype("/System/Library/Fonts/PingFang.ttc", size)
    except Exception:
        return ImageFont.load_default()


def _fit_contain(img: Image.Image, box_w: int, box_h: int, bg=(245, 245, 245)) -> Image.Image:
    img = img.convert("RGB")
    iw, ih = img.size
    r = min(box_w / iw, box_h / ih)
    nw, nh = max(1, int(iw * r)), max(1, int(ih * r))
    resized = img.resize((nw, nh), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (box_w, box_h), bg)
    x = (box_w - nw) // 2
    y = (box_h - nh) // 2
    canvas.paste(resized, (x, y))
    return canvas


def make_page_contain_with_caption(
    *,
    image: Image.Image,
    title: str,
    caption_lines: List[str],
    invert: bool = False,
    spec: PageSpec = PageSpec(),
) -> Image.Image:
    """One-image-per-page template.

    Layout alternates: image-top + caption-bottom, or caption-top + image-bottom.
    """
    W, H = spec.size
    m = spec.margin
    canvas = Image.new("RGB", (W, H), (250, 248, 245))
    draw = ImageDraw.Draw(canvas)

    cap_h = 360
    img_h = H - cap_h

    title_font = _default_font(54)
    body_font = _default_font(34)

    def draw_caption(x0: int, y0: int, w: int, h: int):
        draw.rectangle([x0, y0, x0 + w, y0 + h], fill=(255, 255, 255))
        tx, ty = x0 + m, y0 + 40
        if title:
            draw.text((tx, ty), title[:22], fill=(15, 15, 15), font=title_font)
            ty += 76
        lines = [l.strip("- •\n ") for l in (caption_lines or []) if l.strip()][:4]
        for l in lines:
            draw.text((tx, ty), f"• {l[:30]}", fill=(60, 60, 60), font=body_font)
            ty += 52

    if not invert:
        # image top
        tile = _fit_contain(image, W - 2 * m, img_h - 2 * m)
        canvas.paste(tile, (m, m))
        draw_caption(0, img_h, W, cap_h)
    else:
        draw_caption(0, 0, W, cap_h)
        tile = _fit_contain(image, W - 2 * m, img_h - 2 * m)
        canvas.paste(tile, (m, cap_h + m))

    return canvas


def make_page_contain(
    *,
    image: Image.Image,
    invert: bool = False,
    spec: PageSpec = PageSpec(),
) -> Image.Image:
    """One-image-per-page template without any text overlays."""
    W, H = spec.size
    m = spec.margin
    canvas = Image.new("RGB", (W, H), (250, 248, 245))

    tile = _fit_contain(image, W - 2 * m, H - 2 * m)
    canvas.paste(tile, (m, m))
    return canvas
