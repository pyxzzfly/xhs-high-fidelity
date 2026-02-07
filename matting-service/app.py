import base64
import io
from typing import Tuple

import numpy as np
from fastapi import FastAPI, File, UploadFile, HTTPException
from PIL import Image

try:
    from rembg import remove
except Exception as exc:  # pragma: no cover
    remove = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None

app = FastAPI(title="XHS Matting Service", version="0.1.0")


def _img_to_b64_png(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _rgba_and_mask_from_any(image: Image.Image) -> Tuple[Image.Image, Image.Image]:
    """Return (rgba, maskL). mask is the alpha channel in L mode."""
    rgba = image.convert("RGBA")
    mask = rgba.split()[-1].convert("L")
    return rgba, mask


@app.get("/health")
def health():
    if remove is None:
        return {"status": "degraded", "rembg": False, "error": str(_IMPORT_ERROR)}
    return {"status": "ok", "rembg": True}


@app.post("/matting")
async def matting(image: UploadFile = File(...)):
    if remove is None:
        raise HTTPException(status_code=500, detail=f"rembg import failed: {_IMPORT_ERROR}")

    raw = await image.read()
    try:
        img = Image.open(io.BytesIO(raw))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid image: {exc}") from exc

    # If it already has useful alpha, keep it (fast path)
    if img.mode in {"RGBA", "LA"}:
        rgba, mask = _rgba_and_mask_from_any(img)
        if mask.getextrema() != (255, 255):
            return {
                "rgba_png_b64": _img_to_b64_png(rgba),
                "mask_png_b64": _img_to_b64_png(mask),
                "width": rgba.size[0],
                "height": rgba.size[1],
                "mode": "passthrough_alpha",
            }

    # rembg
    try:
        # rembg returns bytes (png with alpha)
        out = remove(raw)
        rgba = Image.open(io.BytesIO(out)).convert("RGBA")
        mask = rgba.split()[-1].convert("L")

        # Ensure mask is not empty
        if mask.getextrema() == (0, 0):
            raise RuntimeError("empty alpha mask")

        return {
            "rgba_png_b64": _img_to_b64_png(rgba),
            "mask_png_b64": _img_to_b64_png(mask),
            "width": rgba.size[0],
            "height": rgba.size[1],
            "mode": "rembg",
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"matting failed: {exc}") from exc
