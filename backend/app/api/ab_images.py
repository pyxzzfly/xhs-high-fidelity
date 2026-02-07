from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import uuid
from pathlib import Path
from typing import List

from fastapi import APIRouter, Form, HTTPException
from PIL import Image

from app.services.detail_transfer import transfer_high_frequency_details
from app.services.mask_utils import (
    bbox_dominance_ratio,
    bbox_from_mask_l,
    make_background_edit_mask,
    mask_l_to_png_bytes,
)
from app.services.page_templates import make_page_contain
from app.services.painter_client import PainterClient
from app.services.xhs_image_proxy import fetch_xhs_image
from app.services.matting_client import MattingClient
from app.services.fidelity import paste_foreground_exact

router = APIRouter(tags=["ab"])


def _assets_root() -> Path:
    # repo_root/assets/runs (consistent with app.main StaticFiles mount)
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "assets" / "runs"


def _load_images(urls: List[str]) -> List[Image.Image]:
    imgs: List[Image.Image] = []
    for u in (urls or []):
        data, _ = fetch_xhs_image(u)
        # Convert upfront so images are fully loaded and thread-safe for later parallel processing.
        img = Image.open(io.BytesIO(data)).convert("RGB")
        imgs.append(img)
    if not imgs:
        raise RuntimeError("no images")
    return imgs


def _choose_scene_tokens(*, title: str, bullets: List[str], rng) -> List[str]:
    """Pick a small pool of plausible everyday scenes.

    We deliberately avoid strange/unsafe settings (e.g. subway for alcohol).
    """
    t = f"{title or ''} " + " ".join(str(b or "") for b in (bullets or []))

    def has_any(keys: List[str]) -> bool:
        return any(k in t for k in keys)

    # More specific pools first.
    if has_any(["酒", "啤酒", "红酒", "白酒", "威士忌", "伏特加", "香槟", "鸡尾酒"]):
        pool = [
            "家里餐桌",
            "客厅茶几",
            "厨房台面（像在备餐/聚餐前）",
            "朋友家聚会餐桌角落",
            "家里吧台/餐边柜",
        ]
    elif has_any(["面膜", "精华", "水乳", "防晒", "口红", "粉底", "护肤", "香水", "洁面", "卸妆"]):
        pool = [
            "梳妆台",
            "浴室洗手台",
            "窗边桌面自然光",
            "卧室床头柜",
            "书桌角落（生活化收纳）",
        ]
    elif has_any(["电脑", "键盘", "鼠标", "耳机", "相机", "镜头", "手机", "充电", "路由器"]):
        pool = [
            "书桌",
            "窗边桌面自然光",
            "咖啡店靠窗桌面",
            "客厅电视柜/书架旁",
            "卧室床头柜",
        ]
    elif has_any(["零食", "饮料", "咖啡", "茶", "酸奶", "饼干", "巧克力", "泡面", "速食"]):
        pool = [
            "厨房台面",
            "餐桌",
            "窗边桌面自然光",
            "办公室茶水间台面（正常合理）",
            "客厅茶几",
        ]
    else:
        # Generic safe scenes.
        pool = [
            "窗边桌面自然光",
            "书桌",
            "客厅茶几",
            "厨房台面",
            "卧室床头柜",
        ]

    # Pick a stable subset.
    k = 3 if len(pool) >= 3 else len(pool)
    return rng.sample(pool, k=k) if k else []


@router.post("/ab_images")
async def generate_ab_images(
    image_urls_json: str = Form(..., description="JSON list of image urls"),
    title: str = Form(""),
    bullets_json: str = Form("[]", description="JSON list of bullet strings"),
    style_prompt: str = Form("", description="Optional painter style prompt"),
    style_preset: str = Form("ugc", description="ugc | glossy"),
    b_levels_json: str = Form('["medium","aggressive"]', description='JSON list: medium/aggressive'),
    fidelity_mode: str = Form("pixel", description="pixel | none"),
):
    try:
        urls = json.loads(image_urls_json)
        bullets = json.loads(bullets_json or "[]")
        if not isinstance(urls, list):
            raise ValueError("image_urls_json must be a JSON list")
        if not isinstance(bullets, list):
            bullets = []

        task_id = str(uuid.uuid4())
        run_dir = _assets_root() / task_id
        run_dir.mkdir(parents=True, exist_ok=True)

        base_images = _load_images([str(u) for u in urls])

        # Only B: pixel-fidelity restyle
        painter = PainterClient()
        matting = MattingClient()

        engine = (os.getenv("AB_IMAGES_ENGINE") or "v2_mask").strip().lower()
        if engine not in {"v2_mask", "v1_fullimg_pasteback"}:
            engine = "v2_mask"

        # V2 controls (safe defaults)
        protect_dilate_px = int(os.getenv("AB_MASK_PROTECT_DILATE_PX") or "8")
        protect_dilate_px = max(0, min(64, protect_dilate_px))
        max_ratio_delta = float(os.getenv("AB_MAX_BBOX_RATIO_DELTA") or "0.08")
        max_ratio_delta = max(0.0, min(0.5, max_ratio_delta))

        detail_transfer_on = (os.getenv("AB_DETAIL_TRANSFER") or "1").strip() not in {"0", "false", "False"}
        detail_alpha = float(os.getenv("AB_DETAIL_TRANSFER_ALPHA") or "0.22")
        detail_alpha = max(0.0, min(0.6, detail_alpha))
        detail_blur = float(os.getenv("AB_DETAIL_TRANSFER_BLUR_RADIUS") or "2.0")
        detail_blur = max(0.2, min(10.0, detail_blur))

        # Parse levels
        try:
            b_levels = json.loads(b_levels_json or "[]")
            if not isinstance(b_levels, list) or not b_levels:
                b_levels = ["medium", "aggressive"]
        except Exception:
            b_levels = ["medium", "aggressive"]
        b_levels = [str(x).strip().lower() for x in b_levels if str(x).strip()]
        b_levels = [x for x in b_levels if x in {"medium", "aggressive"}]
        if not b_levels:
            b_levels = ["medium", "aggressive"]

        b_urls_by_level: dict[str, List[str]] = {lvl: [] for lvl in b_levels}
        b_errors_by_level: dict[str, List[str]] = {lvl: [] for lvl in b_levels}

        # Keep "same-person" consistency: pick a global baseline for this run,
        # then add small per-image jitter.
        import random

        rng = random.Random(task_id)
        baseline = {
            # Keep UGC degradation mild; too much blur/noise increases "sticker" feel after fidelity pasteback.
            "noise": rng.uniform(0.018, 0.032),
            "contrast": rng.uniform(0.82, 0.90),
            "saturation": rng.uniform(0.88, 0.96),
            "sharpness": rng.uniform(0.82, 0.92),
            "blur": rng.uniform(0.45, 0.90),
            "wb": rng.uniform(-0.02, 0.06),
            "exposure": rng.uniform(0.96, 1.04),
        }
        # scene tokens: consistent set but rotate per image
        scene_tokens = _choose_scene_tokens(title=title or "", bullets=[str(b) for b in bullets], rng=rng) or ["窗边桌面自然光", "书桌", "客厅茶几"]

        # Shared prompt pieces
        glossy_prompt = (
            "Recreate this as a new Xiaohongshu-style post image. Keep the core subject and meaning, "
            "but change composition, background, color grading, lighting, and texture so it looks like a new post. "
            "No watermark, no extra text."
        )

        ugc_negative = (
            "studio lighting, commercial, ultra polished, beauty retouch, DSLR, bokeh, "
            "CGI, 3d render, perfect skin, over-sharpened, over-saturated, HDR, "
            "watermark, text, caption, logo, subtitles, typography, sticker text, price tag, "
            "collage, cutout, sticker, pasted, floating object, oversized subject, wrong scale, "
            "wrong shadow, bad shadow, wrong perspective, floating, sticker-like edges"
        )

        # V2 caches (avoid duplicate matting/mask work for medium/aggressive).
        import threading

        idx_locks = [threading.Lock() for _ in range(len(base_images))]
        base_png_cache: dict[int, bytes] = {}
        product_mask_png_cache: dict[int, bytes] = {}
        edit_mask_png_cache: dict[int, bytes] = {}
        input_ratio_cache: dict[int, float] = {}

        def _get_v2_assets(idx: int) -> tuple[bytes, bytes, bytes, float]:
            """Return (base_png, product_mask_png, edit_mask_png, input_ratio)."""
            with idx_locks[idx]:
                if idx in edit_mask_png_cache:
                    return (
                        base_png_cache[idx],
                        product_mask_png_cache[idx],
                        edit_mask_png_cache[idx],
                        input_ratio_cache.get(idx, 0.0),
                    )

                img0 = base_images[idx]
                buf0 = io.BytesIO()
                img0.convert("RGB").save(buf0, format="PNG")
                base_png = buf0.getvalue()

                # Must succeed in V2 (fidelity-first).
                try:
                    _, product_mask_l = matting.matting(base_png, filename=f"xhs_{idx+1:02d}.png")
                except Exception as exc:
                    raise RuntimeError(f"matting failed for image #{idx+1}: {exc}") from exc

                product_mask_l = product_mask_l.convert("L")
                product_mask_png = mask_l_to_png_bytes(product_mask_l)

                bbox = bbox_from_mask_l(product_mask_l, threshold=128)
                input_ratio = bbox_dominance_ratio(bbox, size=product_mask_l.size)

                edit_mask_l = make_background_edit_mask(
                    product_mask_l,
                    threshold=128,
                    protect_dilate_px=protect_dilate_px,
                )
                edit_mask_png = mask_l_to_png_bytes(edit_mask_l)

                base_png_cache[idx] = base_png
                product_mask_png_cache[idx] = product_mask_png
                edit_mask_png_cache[idx] = edit_mask_png
                input_ratio_cache[idx] = input_ratio
                return base_png, product_mask_png, edit_mask_png, input_ratio

        def _seed_int(*parts: object) -> int:
            h = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).digest()
            return int.from_bytes(h[:8], byteorder="big", signed=False)

        def _build_prompt(*, idx: int, lvl: str) -> str:
            if style_prompt.strip():
                return style_prompt.strip()
            if engine == "v2_mask" and (style_preset or "ugc") == "glossy":
                # Keep subject stable; only rewrite background (cleaner look).
                return (
                    "基于输入图做『背景局部改写』：只改背景和道具，不要改变主体/产品本身（形状、大小比例、文字、logo尽量保持）。"
                    "整体更干净精修但仍真实自然：光源方向一致，接触阴影自然，透视正确，避免漂浮和贴图感。"
                    "不要水印，不要叠加文字/字幕/贴纸。主体在画面中的占比不要变大。"
                )
            if (style_preset or "ugc") == "glossy":
                return glossy_prompt

            scene = scene_tokens[idx % len(scene_tokens)]
            strength_hint = "中等变化" if lvl == "medium" else "更明显的变化"

            if engine == "v2_mask":
                # Mask-edit mode: keep product stable; only rewrite background.
                return (
                    "基于输入图做『背景局部改写』：只改背景和道具，不要改变主体/产品本身（形状、大小比例、文字、logo尽量保持）。"
                    f"背景换成更真实生活感、不过分干净的场景（{scene}），{strength_hint}。"
                    "保持相机视角与透视一致，光源方向一致，接触阴影自然，避免漂浮和贴图感。"
                    "不要新增大面积遮挡主体的物体；不要添加水印；不要叠加文字/字幕/贴纸。"
                    "主体在画面中的占比不要变大，尽量保持相同或略小。"
                )

            # V1: full img2img (less strict)
            return (
                f"把这张图改写成『真实生活感』风格：手机随手拍，生活场景（{scene}），{strength_hint}。"
                "不要棚拍、不要商业精修、不要过度锐化、不要HDR。"
                "保留核心主体与含义，但换背景/色调/光线，让它像同一个用户在不同场景拍的。"
                "不要水印，不要叠加文字/字幕/贴纸/标题栏/价格标签（主体包装自带文字可以保留）。"
                "主体在画面中的比例不要比原图更大，尽量保持相同或略小（避免近景放大特写）。"
                "必须符合真实世界的物理规律：透视正确、光源一致、有自然接触阴影/投影，避免漂浮、穿模和贴图感。"
            )

        def _process_one(*, idx: int, lvl: str) -> tuple[int, str, str, str | None]:
            """Return (idx, lvl, url, err). Runs in a thread (blocking)."""
            import random

            # Use a per-task RNG to be thread-safe and deterministic under parallel execution.
            rng_local = random.Random(_seed_int(task_id, idx, lvl))

            img0 = base_images[idx]
            restyled_img = img0.copy()
            err = None

            if engine != "v2_mask":
                # geometric jitter first (composition/viewpoint)
                try:
                    from app.services.geom_jitter import apply_geom_jitter

                    restyled_img = apply_geom_jitter(restyled_img, rng_local, lvl)
                except Exception:
                    pass

            # Pixel-level fidelity: cut out the main foreground/product and paste back later.
            fg_rgba = None
            fg_mask = None
            if engine != "v2_mask" and (fidelity_mode or "pixel") == "pixel":
                try:
                    bufm = io.BytesIO()
                    restyled_img.convert("RGB").save(bufm, format="PNG")
                    fg_rgba, fg_mask = matting.matting(bufm.getvalue(), filename=f"xhs_{idx+1:02d}.png")
                except Exception:
                    fg_rgba, fg_mask = None, None

            if painter.configured:
                try:
                    prompt = _build_prompt(idx=idx, lvl=lvl)

                    if engine == "v2_mask":
                        base_png, product_mask_png, edit_mask_png, input_ratio = _get_v2_assets(idx)
                        product_mask_l = Image.open(io.BytesIO(product_mask_png)).convert("L")

                        # V2 uses mask-edit (background-only) instead of full img2img pasteback.
                        if lvl == "aggressive":
                            g = 6.3
                            steps = 34
                            ps = 0.70
                        else:
                            g = 6.2
                            steps = 30
                            ps = 0.58

                        def _run_v2(ps_use: float) -> Image.Image:
                            out_bytes = painter.edit(
                                image_bytes=base_png,
                                mask_bytes=edit_mask_png,
                                prompt=prompt,
                                negative_prompt=ugc_negative if (style_preset or "ugc") == "ugc" else "",
                                guidance_scale=g,
                                num_inference_steps=steps,
                                prompt_strength=ps_use,
                                output_format="png",
                            )
                            out_img = Image.open(io.BytesIO(out_bytes)).convert("RGB")
                            if detail_transfer_on and detail_alpha > 0:
                                out_img = transfer_high_frequency_details(
                                    base_rgb=img0,
                                    out_rgb=out_img,
                                    product_mask_l=product_mask_l,
                                    alpha=detail_alpha,
                                    blur_radius=detail_blur,
                                    threshold=128,
                                    inner_erode_px=4,
                                )
                            return out_img

                        def _ratio_for(img: Image.Image) -> float | None:
                            try:
                                bufm2 = io.BytesIO()
                                img.convert("RGB").save(bufm2, format="PNG")
                                _, m2 = matting.matting(bufm2.getvalue(), filename=f"xhs_out_{idx+1:02d}.png")
                                bbox2 = bbox_from_mask_l(m2.convert("L"), threshold=128)
                                return bbox_dominance_ratio(bbox2, size=m2.size)
                            except Exception:
                                return None

                        out_img = _run_v2(ps)

                        # Scale/quality gate (best-effort): avoid subject size drifting wildly.
                        if lvl == "aggressive" and input_ratio > 0 and max_ratio_delta > 0:
                            r1 = _ratio_for(out_img)
                            if r1 is not None and abs(r1 - input_ratio) > max_ratio_delta:
                                ps2 = max(0.35, ps - 0.12)
                                out2 = _run_v2(ps2)
                                r2 = _ratio_for(out2)
                                if r2 is not None and abs(r2 - input_ratio) > max_ratio_delta:
                                    # Fallback to medium strength to keep realism if aggressive keeps drifting.
                                    out3 = _run_v2(0.58)
                                    err = f"scale_gate_fallback(in={input_ratio:.3f}, out={r2:.3f})"
                                    out_img = out3
                                else:
                                    out_img = out2
                        restyled_img = out_img
                    else:
                        # V1 pipeline: full img2img + optional UGC degrade + pixel pasteback.
                        buf = io.BytesIO()
                        restyled_img.convert("RGB").save(buf, format="PNG")
                        img_bytes = buf.getvalue()

                        if lvl == "aggressive":
                            g = 6.4
                            steps = 30
                            ps = 0.80
                        else:
                            g = 6.2
                            steps = 26
                            ps = 0.72

                        out_bytes = painter.img2img(
                            image_bytes=img_bytes,
                            prompt=prompt,
                            negative_prompt=ugc_negative if (style_preset or "ugc") == "ugc" else "",
                            guidance_scale=g,
                            num_inference_steps=steps,
                            prompt_strength=ps,
                            output_format="png",
                        )
                        restyled_img = Image.open(io.BytesIO(out_bytes)).convert("RGB")

                        # Post-degrade to more UGC
                        if (style_preset or "ugc") == "ugc":
                            from app.services.ugc_degrade import apply_ugc_degrade

                            j = lambda a, b: rng_local.uniform(a, b)
                            restyled_img = apply_ugc_degrade(
                                restyled_img,
                                noise_strength=max(0.0, baseline["noise"] + j(-0.008, 0.014)),
                                contrast=min(0.95, max(0.65, baseline["contrast"] + j(-0.04, 0.03))),
                                saturation=min(1.05, max(0.65, baseline["saturation"] + j(-0.05, 0.05))),
                                sharpness=min(1.05, max(0.55, baseline["sharpness"] + j(-0.08, 0.05))),
                                blur_radius=min(2.0, max(0.3, baseline["blur"] + j(-0.30, 0.35))),
                                wb_shift=min(0.14, max(-0.12, baseline["wb"] + j(-0.04, 0.04))),
                                exposure=min(1.12, max(0.88, baseline["exposure"] + j(-0.04, 0.04))),
                                rotate_deg=j(-1.6, 1.6),
                            )

                        # Pixel-fidelity pasteback
                        if (fidelity_mode or "pixel") == "pixel" and fg_rgba is not None and fg_mask is not None:
                            try:
                                from PIL import ImageChops

                                from app.services.harmonize import despill, feather_alpha
                                from app.services.shadow import ShadowService

                                mask = fg_mask.convert("L")
                                # Feather alpha edges a bit to reduce hard cutout look.
                                mask_feather = feather_alpha(mask, radius=3)
                                fg2 = despill(fg_rgba, mask_feather, strength=0.25)

                                # Add a subtle contact shadow to ground the subject.
                                shadow = ShadowService().create_contact_shadow(
                                    mask,
                                    band_ratio=0.10,
                                    blur_radius=10,
                                    opacity=0.18,
                                    y_offset=2,
                                )
                                shadow_multiplier = ImageChops.invert(shadow).convert("RGB")
                                bg2 = ImageChops.multiply(restyled_img.convert("RGB"), shadow_multiplier)

                                restyled_img = paste_foreground_exact(
                                    background_rgb=bg2,
                                    foreground_rgba=fg2,
                                    mask_l=mask_feather,
                                )
                            except Exception:
                                restyled_img = paste_foreground_exact(
                                    background_rgb=restyled_img,
                                    foreground_rgba=fg_rgba,
                                    mask_l=fg_mask,
                                )
                except Exception as exc:
                    err = str(exc)
                    restyled_img = img0
            else:
                err = "Painter not configured"

            # Output image page WITHOUT any text overlays.
            page = make_page_contain(image=restyled_img, invert=(idx % 2 == 1))
            prefix = "BM" if lvl == "medium" else "BA"
            out_path = run_dir / f"{prefix}_{idx+1:02d}.png"
            page.save(out_path)
            url = f"/runs/{task_id}/{out_path.name}"
            return idx, lvl, url, err

        # Parallelize generation: medium/aggressive can be generated concurrently.
        max_conc = int(os.getenv("AB_IMAGES_CONCURRENCY") or "2")
        max_conc = max(1, min(8, max_conc))
        sem = asyncio.Semaphore(max_conc)

        n = len(base_images)
        urls_by_level_idx: dict[str, list[str | None]] = {lvl: [None] * n for lvl in b_levels}

        async def _run_one(idx: int, lvl: str) -> tuple[int, str, str, str | None]:
            async with sem:
                return await asyncio.to_thread(_process_one, idx=idx, lvl=lvl)

        jobs = [_run_one(idx, lvl) for idx in range(n) for lvl in b_levels]
        results = await asyncio.gather(*jobs)
        for idx, lvl, url, err in results:
            urls_by_level_idx[lvl][idx] = url
            if err:
                b_errors_by_level[lvl].append(f"#{idx+1}: {err}")

        for lvl in b_levels:
            # Keep order aligned with the original image list.
            b_urls_by_level[lvl] = [u for u in urls_by_level_idx[lvl] if u]

        (run_dir / "ab_debug.json").write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "b_levels": b_levels,
                    "b": {k: {"count": len(v), "errors": b_errors_by_level.get(k, [])} for k, v in b_urls_by_level.items()},
                    "image_count": len(base_images),
                    "fidelity_mode": fidelity_mode,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        # Return medium/aggressive separately (for UI compare)
        return {
            "task_id": task_id,
            "b_medium_image_urls": b_urls_by_level.get("medium", []),
            "b_aggressive_image_urls": b_urls_by_level.get("aggressive", []),
            "artifacts_dir": str(run_dir),
            "b_error": " | ".join(sum(b_errors_by_level.values(), [])) if any(b_errors_by_level.values()) else None,
            "fidelity_mode": fidelity_mode,
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
