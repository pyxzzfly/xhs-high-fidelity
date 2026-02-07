from __future__ import annotations

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter


def apply_ugc_degrade(
    img: Image.Image,
    *,
    noise_strength: float = 0.035,
    contrast: float = 0.88,
    saturation: float = 0.92,
    sharpness: float = 0.85,
    blur_radius: float = 0.8,
    wb_shift: float = 0.04,
    exposure: float = 1.0,
    rotate_deg: float = 0.0,
) -> Image.Image:
    """Make an image look more like amateur phone photography (UGC).

    Conservative: keep content recognizable; avoid heavy artifacts.

    wb_shift: positive -> warmer, negative -> cooler.
    """
    img = img.convert("RGB")

    # Mild rotation (handheld)
    if rotate_deg and abs(rotate_deg) > 0.01:
        img = img.rotate(float(rotate_deg), resample=Image.Resampling.BICUBIC, expand=False, fillcolor=(245, 245, 245))

    # Low contrast / slightly desaturated / slightly softer
    img = ImageEnhance.Contrast(img).enhance(contrast)
    img = ImageEnhance.Color(img).enhance(saturation)
    img = ImageEnhance.Sharpness(img).enhance(sharpness)
    if exposure and abs(exposure - 1.0) > 1e-3:
        img = ImageEnhance.Brightness(img).enhance(float(exposure))

    if blur_radius and blur_radius > 0:
        img = img.filter(ImageFilter.GaussianBlur(radius=float(blur_radius)))

    arr = np.array(img).astype(np.float32) / 255.0

    # White balance shift (simple): scale R/B
    if wb_shift != 0:
        r = arr[..., 0]
        g = arr[..., 1]
        b = arr[..., 2]
        # warm: boost R, reduce B. cool: opposite.
        r = np.clip(r * (1.0 + wb_shift), 0, 1)
        b = np.clip(b * (1.0 - wb_shift), 0, 1)
        arr[..., 0] = r
        arr[..., 2] = b

    # Add mild sensor noise
    if noise_strength and noise_strength > 0:
        noise = np.random.normal(0.0, float(noise_strength), size=arr.shape).astype(np.float32)
        arr = np.clip(arr + noise, 0.0, 1.0)

    out = (arr * 255.0).clip(0, 255).astype(np.uint8)
    return Image.fromarray(out, mode="RGB")
