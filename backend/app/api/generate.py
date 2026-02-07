from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from typing import Optional
import base64
import io
from PIL import Image
import uuid
import os
import json
from pathlib import Path

from app.services.shadow import ShadowService
from app.services.compositor import Compositor
from app.services.vision import VisionService
from app.services.matting_client import MattingClient
from app.services.reference_analysis import ReferenceAnalyzer
from app.core.logger import TaskLogger

router = APIRouter()

shadow_service = ShadowService()
compositor = Compositor()
vision_service = VisionService()
matting_client = MattingClient()
reference_analyzer = ReferenceAnalyzer(
    vision=vision_service,
    prompts_dir=str(Path(__file__).resolve().parents[1] / "prompts"),
)

OUTPUT_ROOT = Path(os.getenv("XHS_HF_OUTPUT_DIR", Path(__file__).resolve().parents[3] / "assets" / "runs"))
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)


def _img_to_b64_png(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _ensure_product_rgba(prod_img: Image.Image, prod_bytes: bytes) -> Image.Image:
    """Ensure we have a product cutout RGBA.

    Priority:
    1) If input already has non-trivial alpha -> use it.
    2) Else call matting sidecar service (rembg on Python 3.11).

    This is required to *guarantee* logo/text fidelity by isolating product pixels.
    """
    if prod_img.mode != "RGBA":
        prod_rgba = prod_img.convert("RGBA")
    else:
        prod_rgba = prod_img

    alpha = prod_rgba.split()[-1]
    if alpha.getextrema() != (255, 255):
        return prod_rgba

    # No alpha -> sidecar matting
    try:
        rgba, _mask = matting_client.matting(prod_bytes, filename="product.png")
        return rgba.convert("RGBA")
    except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=(
                    "product_image has no alpha, and matting sidecar is not available. "
                    "Please start matting-service on http://127.0.0.1:8911 (or set MATTING_BASE_URL/MATTING_URL). "
                    f"matting_error={exc}"
                ),
            ) from exc


def _extract_alpha_mask(prod_rgba: Image.Image) -> Image.Image:
    if prod_rgba.mode != "RGBA":
        prod_rgba = prod_rgba.convert("RGBA")
    return prod_rgba.split()[-1].convert("L")


def _place_by_position(bg_size: tuple[int, int], prod_size: tuple[int, int], position: str) -> tuple[int, int]:
    bw, bh = bg_size
    pw, ph = prod_size
    pos = (position or "center").strip().lower()

    if pos == "left_half":
        return (0, max(0, (bh - ph) // 2))
    if pos == "right_half":
        return (max(0, bw - pw), max(0, (bh - ph) // 2))
    if pos == "top_half":
        return (max(0, (bw - pw) // 2), 0)
    if pos == "bottom_half":
        return (max(0, (bw - pw) // 2), max(0, bh - ph))

    # default center
    return (max(0, (bw - pw) // 2), max(0, (bh - ph) // 2))


def generate_one(
    *,
    prod_bytes: bytes,
    ref_bytes: bytes,
    prompt: Optional[str] = None,
    scale: float = 0.72,
    shadow: bool = True,
    align_reference: bool = True,
    exaggeration_level: str = "明显",
) -> dict:
    """Synchronous core generator used by both /generate and Flow batch.

    Guarantees product fidelity by:
    - extracting/creating a product RGBA cutout (via matting sidecar if needed)
    - compositing the product pixels on top at the final step
    """

    from app.services.placement import suggest_position_and_scale

    task_id = str(uuid.uuid4())
    logger = TaskLogger(trace_id=task_id)

    run_dir = OUTPUT_ROOT / task_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Load images
    try:
        prod_img = Image.open(io.BytesIO(prod_bytes))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid product_image: {exc}") from exc

    try:
        ref_img = Image.open(io.BytesIO(ref_bytes)).convert("RGB")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid reference_image: {exc}") from exc

    # Save reference
    try:
        ref_img.save(run_dir / "reference.png")
    except Exception:
        pass

    # Matting (guarantee RGBA)
    prod_rgba = _ensure_product_rgba(prod_img, prod_bytes)
    prod_rgba.save(run_dir / "product_rgba.png")
    prod_mask = _extract_alpha_mask(prod_rgba)
    prod_mask.save(run_dir / "product_mask.png")

    # Basic prompt inference (optional, used for future background generation)
    scene_info = {}
    if not prompt:
        scene_info = vision_service.analyze_reference(ref_img)
        prompt = scene_info.get("scene_description", "clean commercial product photo background")

    # Reference analysis (model if available; else heuristic)
    analysis = {
        "mode": "off",
        "hero_position": "center",
        "scale": float(scale),
        "layout_info": {},
        "scene_desc": "",
        "scale_info": {},
    }

    if align_reference:
        try:
            analysis = reference_analyzer.analyze(
                prod_rgba=prod_rgba,
                ref_img=ref_img,
                exaggeration_level=exaggeration_level,
                logger=logger,
            )
        except Exception as exc:
            # If vision isn't configured, do heuristic placement instead of giving up.
            hero_position, heur_scale = suggest_position_and_scale(
                ref_img=ref_img,
                product_rgba=prod_rgba,
                exaggeration_level=exaggeration_level,
            )
            analysis = {
                **analysis,
                "mode": "heuristic",
                "hero_position": hero_position,
                "scale": heur_scale,
                "error": str(exc),
            }

    bw, bh = ref_img.size

    # Replacement background: remove the original product from reference, then insert new product.
    # We use matting-service on the reference too to approximate the foreground (old product) mask.
    bg = ref_img.copy()
    bg.save(run_dir / "bg_reference.png")

    target_bbox = None
    try:
        from app.services.reference_replace import bbox_from_mask, inpaint_remove_foreground, place_product_by_bbox

        # Get an approximate foreground mask from rembg for the reference image
        ref_bytes = ref_img.convert("RGB")
        # Encode reference as PNG bytes for matting sidecar
        buf = io.BytesIO()
        ref_bytes.save(buf, format="PNG")
        ref_png_bytes = buf.getvalue()

        _ref_rgba, ref_mask = matting_client.matting(ref_png_bytes, filename="reference.png")
        ref_mask = ref_mask.resize(ref_img.size)
        ref_mask.save(run_dir / "reference_foreground_mask.png")

        target_bbox = bbox_from_mask(ref_mask)
        if target_bbox is not None:
            bg, inpaint_debug = inpaint_remove_foreground(
                ref_img,
                ref_mask,
                radius=max(3, int(0.010 * min(bw, bh))),
            )
            bg.save(run_dir / "bg_inpainted.png")
            try:
                (run_dir / "inpaint_debug.json").write_text(json.dumps(inpaint_debug, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass

            prod_resized, pos = place_product_by_bbox(
                product_rgba=prod_rgba,
                ref_size=bg.size,
                target_bbox=target_bbox,
                exaggeration_level=exaggeration_level,
            )
        else:
            raise RuntimeError("reference bbox not found")
    except Exception:
        # Fallback: old behavior (paste onto reference)
        final_scale = float(analysis.get("scale", scale) or scale)
        final_scale = max(0.1, min(final_scale, 0.98))
        hero_position = str(analysis.get("hero_position", "center") or "center")

        target_w = max(1, int(bw * final_scale))
        ratio = target_w / max(1, prod_rgba.size[0])
        target_h = max(1, int(prod_rgba.size[1] * ratio))
        prod_resized = prod_rgba.resize((target_w, target_h), Image.Resampling.LANCZOS)
        pos = _place_by_position(bg.size, prod_resized.size, hero_position)

    # Persist analysis
    try:
        (run_dir / "analysis.json").write_text(
            json.dumps(analysis, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass

    # Harmonize to reduce sticker look
    try:
        from app.services.harmonize import feather_alpha, despill, color_match_product

        prod_alpha = _extract_alpha_mask(prod_resized)
        prod_alpha = feather_alpha(prod_alpha, radius=max(2, int(0.006 * min(bw, bh))))
        prod_rgb = despill(prod_resized, prod_alpha, strength=0.35).convert("RGB")
        prod_rgb = color_match_product(
            product_rgb=prod_rgb,
            product_alpha=prod_alpha,
            background_rgb=bg,
            product_pos=pos,
            match_strength=0.55,
        )
        prod_resized = Image.merge("RGBA", (*prod_rgb.split(), prod_alpha))
        prod_resized.save(run_dir / "product_harmonized.png")
    except Exception:
        pass

    # Shadow mask (drop + contact)
    shadow_mask = Image.new("L", bg.size, 0)
    if shadow:
        placed_mask = Image.new("L", bg.size, 0)
        placed_mask.paste(_extract_alpha_mask(prod_resized), pos)

        drop = shadow_service.create_drop_shadow(
            placed_mask,
            offset=(int(0.03 * bw), int(0.03 * bh)),
            blur_radius=max(10, int(0.03 * min(bw, bh))),
            opacity=0.40,
            grow=2,
        )
        contact = shadow_service.create_contact_shadow(
            placed_mask,
            band_ratio=0.14,
            blur_radius=max(6, int(0.016 * min(bw, bh))),
            opacity=0.65,
            y_offset=max(1, int(0.006 * bh)),
        )

        # Combine: take max darkness per pixel
        import numpy as np
        dm = np.maximum(np.array(drop), np.array(contact)).astype("uint8")
        shadow_mask = Image.fromarray(dm, mode="L")

    shadow_mask.save(run_dir / "shadow_mask.png")

    # Composite
    final = compositor.blend_layers(
        background=bg,
        product_rgba=prod_resized,
        shadow_mask=shadow_mask,
        product_pos=pos,
    )

    # Double-safety: paste product again (with harmonized alpha)
    final_rgba = final.convert("RGBA")
    final_rgba.paste(prod_resized, pos, mask=_extract_alpha_mask(prod_resized))
    final = final_rgba.convert("RGB")

    final.save(run_dir / "final.png")

    # Prefer URL to avoid large payloads (prevents gateway/websocket 1009 "message too big").
    return {
        "task_id": task_id,
        "prompt": prompt,
        "scene_analysis": scene_info,
        "analysis": analysis,
        "image_url": f"/runs/{task_id}/final.png",
        "artifacts_dir": str(run_dir),
    }


@router.post("/generate")
async def generate_image(
    product_image: UploadFile = File(...),
    reference_image: UploadFile = File(...),
    prompt: Optional[str] = Form(None),
    scale: float = Form(0.72),
    shadow: bool = Form(True),
    align_reference: bool = Form(True),
    exaggeration_level: str = Form("明显"),
):
    prod_bytes = await product_image.read()
    ref_bytes = await reference_image.read()

    out = generate_one(
        prod_bytes=prod_bytes,
        ref_bytes=ref_bytes,
        prompt=prompt,
        scale=scale,
        shadow=shadow,
        align_reference=align_reference,
        exaggeration_level=exaggeration_level,
    )

    return out


@router.post("/generate_copy")
async def generate_copy_endpoint(
    product_name: str = Form(...),
    features: str = Form(...),
    reference_text: str = Form(""),
):
    return vision_service.generate_xhs_copy(product_name, features, reference_text)
