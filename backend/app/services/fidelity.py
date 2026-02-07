from __future__ import annotations

import io
from typing import Tuple

from PIL import Image


def paste_foreground_exact(
    *,
    background_rgb: Image.Image,
    foreground_rgba: Image.Image,
    mask_l: Image.Image,
) -> Image.Image:
    """Paste foreground pixels back onto background with exact RGB fidelity.

    background_rgb: output image (RGB or RGBA)
    foreground_rgba: original cutout (RGBA)
    mask_l: L mask aligned with foreground/background size
    """
    bg = background_rgb.convert("RGBA")
    fg = foreground_rgba.convert("RGBA")
    m = mask_l.convert("L")

    if bg.size != fg.size:
        fg = fg.resize(bg.size, Image.Resampling.LANCZOS)
        m = m.resize(bg.size, Image.Resampling.LANCZOS)

    bg.paste(fg, (0, 0), mask=m)
    return bg.convert("RGB")
