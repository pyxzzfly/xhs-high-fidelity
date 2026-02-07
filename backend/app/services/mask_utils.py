from __future__ import annotations

import io
from typing import Optional

from PIL import Image, ImageFilter


def _to_bin_l(mask_l: Image.Image, *, threshold: int = 128) -> Image.Image:
    m = mask_l.convert("L")
    return m.point(lambda p: 255 if p >= threshold else 0, mode="L")


def make_background_edit_mask(
    product_mask_l: Image.Image,
    *,
    threshold: int = 128,
    protect_dilate_px: int = 8,
) -> Image.Image:
    """Create an edit mask for Painter.

    Convention we assume: white = editable, black = protected.
    So we invert a (dilated) product mask to allow editing only the background.
    """
    product_bin = _to_bin_l(product_mask_l, threshold=threshold)
    if protect_dilate_px and protect_dilate_px > 0:
        # square structuring element; good enough to protect edges
        size = int(protect_dilate_px) * 2 + 1
        if size >= 3:
            product_bin = product_bin.filter(ImageFilter.MaxFilter(size=size))

    # invert: background=255 (editable), product=0 (protected)
    return product_bin.point(lambda p: 255 - p, mode="L")


def erode_bin_mask_l(mask_bin_l: Image.Image, *, erode_px: int) -> Image.Image:
    if not erode_px or erode_px <= 0:
        return mask_bin_l
    size = int(erode_px) * 2 + 1
    if size < 3:
        return mask_bin_l
    return mask_bin_l.filter(ImageFilter.MinFilter(size=size))


def mask_l_to_png_bytes(mask_l: Image.Image) -> bytes:
    buf = io.BytesIO()
    mask_l.convert("L").save(buf, format="PNG")
    return buf.getvalue()


def bbox_from_mask_l(mask_l: Image.Image, *, threshold: int = 128) -> Optional[tuple[int, int, int, int]]:
    """Return bbox=(x0,y0,x1,y1) for non-zero pixels of a thresholded mask."""
    b = _to_bin_l(mask_l, threshold=threshold)
    return b.getbbox()


def bbox_dominance_ratio(bbox: tuple[int, int, int, int] | None, *, size: tuple[int, int]) -> float:
    """max(bbox_w/W, bbox_h/H) in [0,1]."""
    if not bbox:
        return 0.0
    w, h = size
    if w <= 0 or h <= 0:
        return 0.0
    x0, y0, x1, y1 = bbox
    bw = max(0, x1 - x0)
    bh = max(0, y1 - y0)
    return max(bw / w, bh / h)

