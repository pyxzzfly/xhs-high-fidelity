import base64
import io
import os
from typing import Tuple

import httpx
from PIL import Image


def _b64_to_image(b64: str) -> Image.Image:
    if "," in b64:
        b64 = b64.split(",", 1)[1]
    data = base64.b64decode(b64)
    return Image.open(io.BytesIO(data))


class MattingClient:
    def __init__(self, base_url: str | None = None):
        # Backward/forward compatible env names:
        # - MATTING_BASE_URL: documented in backend/.env.example
        # - MATTING_URL: legacy name used in some code paths
        env_base = (os.getenv("MATTING_BASE_URL") or "").strip()
        env_legacy = (os.getenv("MATTING_URL") or "").strip()
        self.base_url = base_url or env_base or env_legacy or "http://127.0.0.1:8911"

    def matting(self, image_bytes: bytes, filename: str = "image.png") -> Tuple[Image.Image, Image.Image]:
        """Return (product_rgba, product_mask_L)."""
        url = self.base_url.rstrip("/") + "/matting"
        with httpx.Client(timeout=180) as client:
            resp = client.post(url, files={"image": (filename, image_bytes, "application/octet-stream")})
        resp.raise_for_status()
        payload = resp.json()
        rgba = _b64_to_image(payload["rgba_png_b64"]).convert("RGBA")
        mask = _b64_to_image(payload["mask_png_b64"]).convert("L")
        return rgba, mask
