from __future__ import annotations

from typing import Tuple

import numpy as np
from PIL import Image, ImageFilter

from app.services.mask_utils import _to_bin_l, erode_bin_mask_l


def transfer_high_frequency_details(
    *,
    base_rgb: Image.Image,
    out_rgb: Image.Image,
    product_mask_l: Image.Image,
    alpha: float = 0.22,
    blur_radius: float = 2.0,
    threshold: int = 128,
    inner_erode_px: int = 4,
) -> Image.Image:
    """Blend high-frequency details from base image into output inside product region.

    This helps recover packaging text/edges while keeping the global lighting/color from out_rgb.
    """
    if alpha <= 0:
        return out_rgb.convert("RGB")

    out_img = out_rgb.convert("RGB")
    ow, oh = out_img.size

    base_rs = base_rgb.convert("RGB").resize((ow, oh), Image.Resampling.LANCZOS)
    mask_rs = product_mask_l.convert("L").resize((ow, oh), Image.Resampling.NEAREST)

    product_bin = _to_bin_l(mask_rs, threshold=threshold)
    product_inner = erode_bin_mask_l(product_bin, erode_px=inner_erode_px)

    m = np.asarray(product_inner, dtype=np.uint8)
    mask_bool = m >= 128
    if not mask_bool.any():
        return out_img

    base_arr = np.asarray(base_rs, dtype=np.float32) / 255.0
    out_arr_u8 = np.asarray(out_img, dtype=np.uint8).copy()
    out_arr = out_arr_u8.astype(np.float32) / 255.0

    base_blur = np.asarray(
        base_rs.filter(ImageFilter.GaussianBlur(radius=float(blur_radius))),
        dtype=np.float32,
    ) / 255.0

    detail = base_arr - base_blur

    # Only write back masked region; outside stays byte-identical (no rounding drift).
    modified = np.clip(out_arr + detail * float(alpha), 0.0, 1.0)
    out_arr_u8[mask_bool] = (modified * 255.0).astype(np.uint8)[mask_bool]
    return Image.fromarray(out_arr_u8, mode="RGB")
