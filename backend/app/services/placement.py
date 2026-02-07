from __future__ import annotations

import numpy as np
from PIL import Image, ImageFilter


def _to_gray_arr(img: Image.Image) -> np.ndarray:
    return np.array(img.convert("L"), dtype=np.float32) / 255.0


def suggest_position_and_scale(
    ref_img: Image.Image,
    product_rgba: Image.Image,
    exaggeration_level: str = "明显",
) -> tuple[str, float]:
    """Heuristic reference-aware placement without any vision API.

    - Position: pick the cleanest region among left/right/top/bottom/center using edge density.
    - Scale: choose target width ratio by exaggeration_level.

    Returns: (hero_position, scale)
    """

    # 1) scale targets (width ratio)
    level = (exaggeration_level or "明显").strip()
    target = {"轻微": 0.62, "明显": 0.72, "强烈": 0.84}.get(level, 0.72)

    # 2) edge density map (lower = emptier)
    gray = ref_img.convert("L")
    # emphasize edges: difference from blurred version
    blurred = gray.filter(ImageFilter.GaussianBlur(radius=max(2, min(ref_img.size) // 200)))
    edge = Image.fromarray(np.clip(np.abs(np.array(gray, dtype=np.int16) - np.array(blurred, dtype=np.int16)), 0, 255).astype(np.uint8))
    edge_arr = np.array(edge, dtype=np.float32) / 255.0

    h, w = edge_arr.shape

    regions = {
        "left_half": (0, 0, w // 2, h),
        "right_half": (w // 2, 0, w, h),
        "top_half": (0, 0, w, h // 2),
        "bottom_half": (0, h // 2, w, h),
        "center": (w // 4, h // 4, 3 * w // 4, 3 * h // 4),
    }

    def score(box: tuple[int, int, int, int]) -> float:
        x1, y1, x2, y2 = box
        patch = edge_arr[y1:y2, x1:x2]
        if patch.size == 0:
            return 1e9
        # mean edge density
        return float(patch.mean())

    best_pos = min(regions.keys(), key=lambda k: score(regions[k]))

    # Don't pick center if it's busy and left/right is much cleaner
    best_score = score(regions[best_pos])
    center_score = score(regions["center"])
    if best_pos == "center":
        # ok
        pass
    else:
        # if center is very clean, prefer center
        if center_score <= best_score * 1.15:
            best_pos = "center"

    return best_pos, float(target)
