from __future__ import annotations

import io

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter


def apply_ugc_degrade(
    img: Image.Image,
    *,
    noise_strength: float = 0.035,
    chroma_noise: float = 0.012,
    noise_shadow_boost: float = 0.75,
    contrast: float = 0.88,
    saturation: float = 0.92,
    sharpness: float = 0.85,
    blur_radius: float = 0.8,
    wb_shift: float = 0.04,
    exposure: float = 1.0,
    rotate_deg: float = 0.0,
    vignette: float = 0.06,
    jpeg_quality: int | None = 88,
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

    # Vignette (common in phone photos / imperfect lighting)
    if vignette and vignette > 1e-6:
        h, w = arr.shape[:2]
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
        rr = ((xx - cx) / max(1.0, cx)) ** 2 + ((yy - cy) / max(1.0, cy)) ** 2
        rr = np.clip(rr, 0.0, 1.5)
        # Darken edges slightly.
        v = 1.0 - float(vignette) * (rr ** 1.15)
        v = np.clip(v, 0.78, 1.0).astype(np.float32)
        arr = np.clip(arr * v[..., None], 0.0, 1.0)

    # Add mild sensor noise (stronger in shadows, plus a small chroma component).
    if (noise_strength and noise_strength > 0) or (chroma_noise and chroma_noise > 0):
        # Luma for shadow weighting
        luma = (0.2126 * arr[..., 0] + 0.7152 * arr[..., 1] + 0.0722 * arr[..., 2]).astype(np.float32)
        shadow_w = 1.0 + float(noise_shadow_boost) * (1.0 - luma) ** 2.2
        shadow_w = shadow_w[..., None]

        if noise_strength and noise_strength > 0:
            n_luma = np.random.normal(0.0, float(noise_strength), size=arr.shape[:2]).astype(np.float32)[..., None]
            arr = arr + n_luma * shadow_w

        if chroma_noise and chroma_noise > 0:
            n_rgb = np.random.normal(0.0, float(chroma_noise), size=arr.shape).astype(np.float32)
            arr = arr + n_rgb * shadow_w

        arr = np.clip(arr, 0.0, 1.0)

    out = (arr * 255.0).clip(0, 255).astype(np.uint8)
    out_img = Image.fromarray(out, mode="RGB")

    # JPEG round-trip: add mild phone compression artifacts and unify texture.
    if isinstance(jpeg_quality, int):
        q = int(jpeg_quality)
        q = max(60, min(96, q))
        buf = io.BytesIO()
        # subsampling=2 approximates common 4:2:0 phone JPEG.
        out_img.save(buf, format="JPEG", quality=q, subsampling=2, optimize=True)
        buf.seek(0)
        out_img = Image.open(buf).convert("RGB")

    return out_img
