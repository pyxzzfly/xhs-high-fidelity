from __future__ import annotations

import numpy as np
from PIL import Image, ImageFilter


def feather_alpha(alpha: Image.Image, radius: int = 3) -> Image.Image:
    """Feather alpha edges to reduce cutout look."""
    if alpha.mode != "L":
        alpha = alpha.convert("L")
    if radius <= 0:
        return alpha
    # Slight blur on alpha edges; keep interior mostly opaque
    return alpha.filter(ImageFilter.GaussianBlur(radius=radius))


def despill(product_rgba: Image.Image, alpha: Image.Image, strength: float = 0.35) -> Image.Image:
    """Simple edge despill: reduce green/white fringe on semi-transparent pixels.

    This is conservative and only affects pixels where alpha is not fully opaque.
    """
    if product_rgba.mode != "RGBA":
        product_rgba = product_rgba.convert("RGBA")
    a = np.array(alpha.convert("L"), dtype=np.float32) / 255.0
    rgba = np.array(product_rgba, dtype=np.float32)

    # Edge mask: where alpha is between 0 and 1
    edge = (a > 0.02) & (a < 0.98)
    if not edge.any():
        return product_rgba

    # Reduce excessive green channel on edges; also slightly darken towards bg neutrality
    r = rgba[..., 0]
    g = rgba[..., 1]
    b = rgba[..., 2]

    # Target green should not exceed max(r,b) by too much
    max_rb = np.maximum(r, b)
    excess = np.maximum(0.0, g - max_rb)

    g2 = g - excess * (strength * 1.5)
    # Slightly pull RGB towards their mean on edges
    mean = (r + g2 + b) / 3.0
    r2 = r * (1 - strength) + mean * strength
    b2 = b * (1 - strength) + mean * strength

    rgba[..., 0][edge] = r2[edge]
    rgba[..., 1][edge] = g2[edge]
    rgba[..., 2][edge] = b2[edge]

    rgba = np.clip(rgba, 0, 255).astype(np.uint8)
    return Image.fromarray(rgba, mode="RGBA")


def _stats(arr: np.ndarray, mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    # arr: HxWx3 float32, mask: HxW bool
    pix = arr[mask]
    if pix.size == 0:
        mu = arr.reshape(-1, 3).mean(axis=0)
        sig = arr.reshape(-1, 3).std(axis=0) + 1e-6
        return mu, sig
    mu = pix.mean(axis=0)
    sig = pix.std(axis=0) + 1e-6
    return mu, sig


def color_match_product(
    product_rgb: Image.Image,
    product_alpha: Image.Image,
    background_rgb: Image.Image,
    product_pos: tuple[int, int],
    match_strength: float = 0.6,
) -> Image.Image:
    """Match product color/brightness to surrounding background region.

    We do a simple mean/std match in RGB space using a ring region around the product bbox.
    This keeps textures but aligns tone, reducing the 'sticker' feel.
    """

    bg = background_rgb.convert("RGB")
    prod = product_rgb.convert("RGB")
    alpha = product_alpha.convert("L")

    bg_arr = np.array(bg, dtype=np.float32)
    prod_arr = np.array(prod, dtype=np.float32)
    a_arr = np.array(alpha, dtype=np.float32) / 255.0

    x, y = product_pos
    ph, pw = prod_arr.shape[0], prod_arr.shape[1]

    # Background sample box: slightly larger than product bbox
    pad = int(max(8, 0.08 * max(pw, ph)))
    x1 = max(0, x - pad)
    y1 = max(0, y - pad)
    x2 = min(bg_arr.shape[1], x + pw + pad)
    y2 = min(bg_arr.shape[0], y + ph + pad)

    bg_patch = bg_arr[y1:y2, x1:x2]
    if bg_patch.size == 0:
        return prod

    # Build ring mask: patch area minus product area
    ring = np.ones((y2 - y1, x2 - x1), dtype=bool)
    # exclude product rectangle
    rx1 = x - x1
    ry1 = y - y1
    rx2 = rx1 + pw
    ry2 = ry1 + ph
    rx1c, ry1c = max(0, rx1), max(0, ry1)
    rx2c, ry2c = min(ring.shape[1], rx2), min(ring.shape[0], ry2)
    ring[ry1c:ry2c, rx1c:rx2c] = False

    # Compute stats
    bg_mu, bg_sig = _stats(bg_patch.reshape(-1, 3), ring.reshape(-1))

    # Product stats (use only opaque-ish pixels)
    prod_mask = a_arr > 0.5
    prod_mu, prod_sig = _stats(prod_arr.reshape(-1, 3), prod_mask.reshape(-1))

    # Apply match: (x - mu_p)/sig_p * sig_bg + mu_bg
    matched = (prod_arr - prod_mu) / prod_sig * bg_sig + bg_mu
    out = prod_arr * (1 - match_strength) + matched * match_strength
    out = np.clip(out, 0, 255).astype(np.uint8)
    return Image.fromarray(out, mode="RGB")


def edge_only_blend(
    *,
    original_rgb: Image.Image,
    adjusted_rgb: Image.Image,
    alpha_l: Image.Image,
    power: float = 1.6,
) -> Image.Image:
    """Blend adjusted colors only near alpha edges.

    This preserves fully-opaque product pixels (e.g. logo/text) while still allowing
    edge harmonization to reduce cutout/sticker look.
    """
    base = original_rgb.convert("RGB")
    adj = adjusted_rgb.convert("RGB").resize(base.size, Image.Resampling.LANCZOS)
    a = np.array(alpha_l.convert("L").resize(base.size, Image.Resampling.LANCZOS), dtype=np.float32) / 255.0

    # Weight is strongest on semi-transparent edges and near-zero in the opaque interior.
    w = np.clip(1.0 - a, 0.0, 1.0)
    if power and abs(float(power) - 1.0) > 1e-6:
        w = np.power(w, float(power))
    w3 = w[..., None]

    base_arr = np.array(base, dtype=np.float32)
    adj_arr = np.array(adj, dtype=np.float32)
    out = base_arr * (1.0 - w3) + adj_arr * w3
    out = np.clip(out, 0, 255).astype(np.uint8)
    return Image.fromarray(out, mode="RGB")
