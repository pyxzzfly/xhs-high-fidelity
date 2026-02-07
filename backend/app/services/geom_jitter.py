from __future__ import annotations

import random
from typing import Tuple

import numpy as np
from PIL import Image


def _crop_jitter(img: Image.Image, rng: random.Random, scale_min: float, scale_max: float) -> Image.Image:
    img = img.convert("RGB")
    w, h = img.size
    s = rng.uniform(scale_min, scale_max)
    # s < 1 means crop in; s > 1 means pad out (we'll clamp to 1)
    s = min(1.0, max(0.72, s))
    cw, ch = int(w * s), int(h * s)
    cw = max(10, min(cw, w))
    ch = max(10, min(ch, h))

    max_dx = max(0, w - cw)
    max_dy = max(0, h - ch)
    x1 = int(rng.uniform(0, max_dx + 1e-6))
    y1 = int(rng.uniform(0, max_dy + 1e-6))
    crop = img.crop((x1, y1, x1 + cw, y1 + ch))
    return crop.resize((w, h), Image.Resampling.LANCZOS)


def _perspective_jitter(img: Image.Image, rng: random.Random, strength: float) -> Image.Image:
    """Apply a mild perspective warp.

    strength: 0..1 (rough).
    """
    img = img.convert("RGB")
    w, h = img.size
    s = float(strength)
    if s <= 1e-6:
        return img

    # jitter corners inwards/outwards a bit
    dx = w * 0.06 * s
    dy = h * 0.06 * s

    def j(a: float) -> float:
        return rng.uniform(-a, a)

    src = np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype=np.float32)
    dst = np.array(
        [
            [0 + j(dx), 0 + j(dy)],
            [w + j(dx), 0 + j(dy)],
            [w + j(dx), h + j(dy)],
            [0 + j(dx), h + j(dy)],
        ],
        dtype=np.float32,
    )

    # compute perspective transform matrix
    import cv2

    M = cv2.getPerspectiveTransform(src, dst)
    arr = np.array(img)
    out = cv2.warpPerspective(arr, M, (w, h), borderMode=cv2.BORDER_REFLECT)
    return Image.fromarray(out)


def apply_geom_jitter(img: Image.Image, rng: random.Random, level: str) -> Image.Image:
    """Change composition / viewpoint slightly to reduce similarity.

    level: "medium" | "aggressive"
    """
    level = (level or "medium").strip().lower()
    if level not in {"medium", "aggressive"}:
        level = "medium"

    if level == "medium":
        # Keep changes subtle to avoid enlarging the subject (user feedback: product too big).
        out = _crop_jitter(img, rng, 0.93, 1.00)
        out = _perspective_jitter(out, rng, strength=0.28)
        return out

    # aggressive
    # Still avoid heavy zoom-in; rely more on perspective change than cropping.
    out = _crop_jitter(img, rng, 0.88, 0.98)
    out = _perspective_jitter(out, rng, strength=0.45)
    return out
