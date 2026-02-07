from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np
from PIL import Image


@dataclass
class BBox:
    x1: int
    y1: int
    x2: int
    y2: int

    @property
    def w(self) -> int:
        return max(1, self.x2 - self.x1)

    @property
    def h(self) -> int:
        return max(1, self.y2 - self.y1)

    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) / 2

    @property
    def cy(self) -> float:
        return (self.y1 + self.y2) / 2


def bbox_from_mask(mask_l: Image.Image, thr: int = 128) -> Optional[BBox]:
    m = np.array(mask_l.convert("L"))
    ys, xs = np.where(m >= thr)
    if len(xs) < 50:
        return None
    x1, x2 = int(xs.min()), int(xs.max())
    y1, y2 = int(ys.min()), int(ys.max())
    if x2 <= x1 or y2 <= y1:
        return None
    return BBox(x1=x1, y1=y1, x2=x2, y2=y2)


def _scale_for_exaggeration(level: str) -> float:
    level = (level or "明显").strip()
    return {"轻微": 1.00, "明显": 1.08, "强烈": 1.18}.get(level, 1.08)


def place_product_by_bbox(
    *,
    product_rgba: Image.Image,
    ref_size: tuple[int, int],
    target_bbox: BBox,
    exaggeration_level: str,
) -> tuple[Image.Image, tuple[int, int]]:
    """Resize product to fit target bbox (keeping aspect), place by bbox center."""

    bw, bh = ref_size
    prod = product_rgba.convert("RGBA")

    # Fit product into bbox (slightly exaggerated)
    s = _scale_for_exaggeration(exaggeration_level)
    max_w = max(1, int(target_bbox.w * s))
    max_h = max(1, int(target_bbox.h * s))

    ratio = min(max_w / prod.size[0], max_h / prod.size[1])
    new_w = max(1, int(prod.size[0] * ratio))
    new_h = max(1, int(prod.size[1] * ratio))
    prod_resized = prod.resize((new_w, new_h), Image.Resampling.LANCZOS)

    # center align
    x = int(round(target_bbox.cx - new_w / 2))
    y = int(round(target_bbox.cy - new_h / 2))

    # clamp to canvas
    x = max(0, min(x, bw - new_w))
    y = max(0, min(y, bh - new_h))

    return prod_resized, (x, y)


def inpaint_remove_foreground(
    ref_rgb: Image.Image,
    fg_mask_l: Image.Image,
    radius: int = 5,
) -> tuple[Image.Image, dict]:
    """Remove foreground using classical inpainting.

    We try both TELEA and NS and choose the one that better matches the surrounding texture,
    to reduce obvious blurry blocks / artifacts.

    Returns: (best_image, debug)
    debug contains intermediate masks and scores.
    """
    import cv2

    ref = np.array(ref_rgb.convert("RGB"))
    mask0 = np.array(fg_mask_l.convert("L"))
    mask = (mask0 > 128).astype(np.uint8) * 255

    # Clean mask: close small holes, then dilate a bit to cover edges of the old product.
    # Also add a slight downward bias dilation to better cover the contact edge artifacts.
    k = max(3, int(max(ref_rgb.size) * 0.004) // 2 * 2 + 1)  # odd
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)
    mask = cv2.dilate(mask, kernel, iterations=1)

    # downward bias: extend mask a bit more downward to remove base contact remnants
    k2 = max(3, int(max(ref_rgb.size) * 0.006) // 2 * 2 + 1)
    kernel_down = cv2.getStructuringElement(cv2.MORPH_RECT, (k2, k2 * 2))
    mask_down = cv2.dilate(mask, kernel_down, iterations=1)
    mask = cv2.max(mask, mask_down)

    ref_bgr = cv2.cvtColor(ref, cv2.COLOR_RGB2BGR)

    # Determine radius adaptively (bigger object -> bigger radius)
    radius = int(max(3, radius))

    out_telea = cv2.inpaint(ref_bgr, mask, inpaintRadius=float(radius), flags=cv2.INPAINT_TELEA)
    out_ns = cv2.inpaint(ref_bgr, mask, inpaintRadius=float(radius), flags=cv2.INPAINT_NS)

    # Score: compare gradient energy in filled region vs ring region
    def score(out_bgr_img):
        out_gray = cv2.cvtColor(out_bgr_img, cv2.COLOR_BGR2GRAY).astype(np.float32)
        ref_gray = cv2.cvtColor(ref_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)

        # ring = dilated mask - mask
        ring = cv2.dilate(mask, kernel, iterations=2)
        ring = (ring > 0).astype(np.uint8) * 255
        ring = cv2.subtract(ring, mask)

        # compute sobel magnitude
        def grad_mag(gray):
            gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
            gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
            return cv2.magnitude(gx, gy)

        g_out = grad_mag(out_gray)
        g_ref = grad_mag(ref_gray)

        m_fill = mask > 0
        m_ring = ring > 0
        if m_ring.sum() < 50 or m_fill.sum() < 50:
            return 1e9

        fill_energy = float(g_out[m_fill].mean())
        ring_energy = float(g_ref[m_ring].mean())
        # penalize over-smooth (fill too low) and over-sharp (fill too high)
        return abs(fill_energy - ring_energy)

    s_telea = score(out_telea)
    s_ns = score(out_ns)

    best = out_telea if s_telea <= s_ns else out_ns
    best_kind = "telea" if s_telea <= s_ns else "ns"

    best_rgb = cv2.cvtColor(best, cv2.COLOR_BGR2RGB)
    debug = {
        "mask_kernel": int(k),
        "radius": int(radius),
        "score_telea": float(s_telea),
        "score_ns": float(s_ns),
        "best": best_kind,
    }

    return Image.fromarray(best_rgb, mode="RGB"), debug
