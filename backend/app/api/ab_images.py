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
from PIL import Image, ImageFilter

from app.services.detail_transfer import transfer_high_frequency_details
from app.services.mask_utils import (
    bbox_dominance_ratio,
    bbox_from_mask_l,
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


def _infer_product_category(*, title: str, bullets: List[str]) -> str:
    """Infer a coarse product category from rewritten title/bullets.

    Heuristic-only by default (fast, deterministic). This is used to pick more
    plausible everyday scenes so the product looks "lived-in" instead of a
    generic tabletop hero shot.
    """
    text = f"{title}\n" + "\n".join(str(b) for b in (bullets or []))
    text = (text or "").strip().lower()
    if not text:
        return "generic"

    def score(keywords: List[str]) -> int:
        return sum(1 for kw in keywords if kw and kw.lower() in text)

    # NOTE: keep categories broad; we only need scene plausibility, not taxonomy accuracy.
    scores: dict[str, int] = {
        "alcohol": score(["酒", "白酒", "啤酒", "红酒", "黄酒", "洋酒", "威士忌", "伏特加", "朗姆", "劲酒", "鸡尾酒", "小酌"]),
        "beverage": score(["饮料", "果汁", "汽水", "苏打水", "气泡水", "茶", "咖啡", "奶茶", "牛奶", "酸奶", "椰子水", "椰汁"]),
        "snack_food": score(["零食", "饼干", "薯片", "坚果", "巧克力", "糖", "辣条", "泡面", "方便面", "麦片", "果酱", "调味", "酱", "下饭"]),
        "skincare": score(["护肤", "面霜", "乳液", "精华", "防晒", "洗面奶", "洁面", "爽肤水", "面膜", "喷雾", "身体乳", "卸妆"]),
        "cosmetics": score(["口红", "唇釉", "粉底", "遮瑕", "眼影", "腮红", "眉笔", "睫毛", "定妆", "香水"]),
        "home_cleaning": score(["清洁", "洗衣", "洗洁精", "消毒", "除菌", "洁厕", "拖把", "纸巾", "湿巾", "洗衣液", "洗衣粉", "去污"]),
        "baby": score(["婴儿", "宝宝", "奶粉", "尿不湿", "纸尿裤", "辅食", "奶瓶", "孕妇", "宝妈"]),
        "pet": score(["宠物", "猫", "狗", "猫砂", "狗粮", "猫粮", "冻干", "罐头"]),
        "electronics": score(["手机", "耳机", "充电", "充电器", "数据线", "电脑", "键盘", "鼠标", "相机", "镜头", "投影", "手表"]),
        "fashion": score(["衣", "鞋", "包", "裙", "外套", "帽", "围巾", "手链", "项链"]),
        "supplement": score(["维生素", "益生菌", "蛋白", "胶原", "鱼油", "保健", "养生", "补剂", "睡眠", "褪黑素"]),
    }

    # If the text mentions alcohol explicitly, strongly prefer "alcohol" even if other keywords match.
    best_cat = max(scores.items(), key=lambda kv: kv[1])[0]
    best_score = scores.get(best_cat, 0)
    if best_score <= 0:
        return "generic"
    return best_cat


def _choose_scene_tokens(*, title: str, bullets: List[str], rng) -> List[str]:
    """Pick a small pool of plausible everyday scenes.

    Keep it generic (not tied to specific product categories), and avoid strange/unsafe
    settings that easily look "wrong" in real life.
    """
    category = _infer_product_category(title=title, bullets=bullets)

    # Generic safe scenes (fallback / mix-in).
    generic_pool = [
        "窗边桌面自然光",
        "书桌（生活化杂物）",
        "客厅茶几（有杂物）",
        "厨房台面（干净但生活化）",
        "餐桌角落（干净但随手）",
        "卧室床头柜（随手拍）",
        "客厅边柜/置物架旁（家庭感）",
        "阳台/飘窗小桌（生活气息）",
        "玄关柜/鞋柜上（随手一放）",
        "办公室工位桌面（键盘/文件旁）",
    ]

    # Category-specific pools (more "reasonable" props/context).
    by_cat: dict[str, List[str]] = {
        # 酒：吃饭/聚会更自然；避免过度“电商摆拍”
        "alcohol": [
            "家常饭桌（有菜、碗筷、杯子）",
            "朋友小聚客厅茶几（零食/杯子/纸巾）",
            "火锅/烧烤餐桌（烟火气但干净）",
            "餐边柜/厨房吧台（杯子/开瓶器旁）",
            "露台/窗边夜景小桌（暖光小酌氛围）",
            "冰箱旁/餐桌边（随手拿出来拍）",
        ],
        "beverage": [
            "早餐桌（面包/水果/杯子）",
            "下午茶角落（书/笔记本/杯垫）",
            "办公室工位（键盘/文件/水杯旁）",
            "通勤包旁（随手放桌上）",
            "健身后桌面（水杯/毛巾旁）",
        ],
        "snack_food": [
            "追剧客厅茶几（遥控器/纸巾/零食碗）",
            "办公室加班桌（便签/键盘旁）",
            "餐桌角落（家常饭后小零食）",
            "野餐垫/露营小桌（随手拍）",
            "厨房台面（随手拆封）",
        ],
        "skincare": [
            "浴室洗手台（毛巾/洗漱用品旁）",
            "梳妆台（镜子/发夹/化妆棉旁）",
            "床头柜（睡前护肤氛围）",
            "随身化妆包旁（出门补涂）",
        ],
        "cosmetics": [
            "梳妆台（镜子/刷具旁）",
            "包里/桌面（出门补妆随手拍）",
            "床头柜（夜间氛围灯）",
        ],
        "home_cleaning": [
            "厨房水槽边（抹布/洗碗工具旁）",
            "浴室台面（清洁工具旁）",
            "洗衣机/洗衣篮旁（家务场景）",
            "玄关/地面角落（清洁前后对比氛围）",
        ],
        "baby": [
            "婴儿房收纳台（生活化摆放）",
            "餐椅/餐桌一角（辅食时间）",
            "妈咪包旁（出门随手拍）",
        ],
        "pet": [
            "客厅地面（宠物用品旁）",
            "猫爬架/宠物窝旁（生活化）",
            "阳台角落（自然光随手拍）",
        ],
        "electronics": [
            "书桌电脑旁（线材/笔记本旁）",
            "床头柜（夜间台灯/充电场景）",
            "咖啡店小桌（随手拍）",
            "行李箱/出差包旁（出行场景）",
        ],
        "fashion": [
            "玄关镜子旁（出门前随手拍）",
            "衣柜/床边（试穿场景）",
            "沙发角落（随手摆放）",
        ],
        "supplement": [
            "早餐桌（水杯/勺子旁）",
            "书桌一角（规律打卡氛围）",
            "运动包/健身角落（水杯旁）",
        ],
    }

    pool = list(by_cat.get(category) or [])
    # Mix-in generics to keep variety, but keep category scenes dominant when detected.
    for s in generic_pool:
        if s not in pool:
            pool.append(s)

    # Pick a stable subset (rotate per image).
    k = 4 if len(pool) >= 4 else len(pool)
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
        protect_dilate_px = int(os.getenv("AB_MASK_PROTECT_DILATE_PX") or "4")
        protect_dilate_px = max(0, min(64, protect_dilate_px))
        max_ratio_delta = float(os.getenv("AB_MAX_BBOX_RATIO_DELTA") or "0.08")
        max_ratio_delta = max(0.0, min(0.5, max_ratio_delta))

        # In mask-edit mode, the matting mask often contains soft shadows/reflections.
        # Use a stricter "core" threshold to protect only the product itself, and
        # allow Painter to rewrite the shadow/reflection area as part of the background.
        core_threshold = int(os.getenv("AB_PRODUCT_CORE_THRESHOLD") or "224")
        core_threshold = max(128, min(250, core_threshold))
        core_open_px = int(os.getenv("AB_PRODUCT_MASK_OPEN_PX") or "3")
        core_open_px = max(0, min(64, core_open_px))
        # Further shrink the protected core to avoid preserving input shadows/reflections
        # that matting sometimes includes as "foreground".
        #
        # IMPORTANT: pasteback uses the same "core" mask. If this is too large, you get
        # old shadow artifacts; if too small, you risk letting Painter touch logo/text.
        # Default to 0 (fidelity-first); tune per dataset.
        core_erode_px = int(os.getenv("AB_PRODUCT_CORE_ERODE_PX") or "0")
        core_erode_px = max(0, min(128, core_erode_px))

        detail_transfer_on = (os.getenv("AB_DETAIL_TRANSFER") or "1").strip() not in {"0", "false", "False"}
        detail_alpha = float(os.getenv("AB_DETAIL_TRANSFER_ALPHA") or "0.22")
        detail_alpha = max(0.0, min(0.6, detail_alpha))
        detail_blur = float(os.getenv("AB_DETAIL_TRANSFER_BLUR_RADIUS") or "2.0")
        detail_blur = max(0.2, min(10.0, detail_blur))
        detail_threshold = int(os.getenv("AB_DETAIL_TRANSFER_THRESHOLD") or str(core_threshold))
        detail_threshold = max(128, min(250, detail_threshold))
        detail_inner_erode_px = int(os.getenv("AB_DETAIL_TRANSFER_INNER_ERODE_PX") or "8")
        detail_inner_erode_px = max(0, min(64, detail_inner_erode_px))

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
            "noise": rng.uniform(0.020, 0.036),
            "contrast": rng.uniform(0.82, 0.90),
            "saturation": rng.uniform(0.88, 0.96),
            "sharpness": rng.uniform(0.78, 0.90),
            # Keep blur low to preserve label readability; add depth blur later for background if needed.
            "blur": rng.uniform(0.25, 0.60),
            "wb": rng.uniform(-0.02, 0.06),
            "exposure": rng.uniform(0.96, 1.04),
            "vignette": rng.uniform(0.04, 0.10),
            "jpeg_q": int(rng.uniform(82, 90)),
        }
        # Scene tokens: pick more reasonable everyday contexts based on inferred product category.
        scene_category = _infer_product_category(title=title or "", bullets=[str(b) for b in bullets])
        scene_tokens = _choose_scene_tokens(title=title or "", bullets=[str(b) for b in bullets], rng=rng) or [
            "窗边桌面自然光",
            "书桌（生活化杂物）",
            "客厅茶几（有杂物）",
        ]

        def _category_hint(cat: str) -> str:
            cat = (cat or "").strip().lower()
            if cat == "alcohol":
                return "产品是酒类，更适合吃饭/聚会/下酒菜的烟火气场景。可以有碗筷/菜/杯子/纸巾/开瓶器，让人感觉“有人刚在用”，但不要出现清晰人脸/完整人物。"
            if cat == "beverage":
                return "产品是饮品，更适合早餐/下午茶/通勤/工位的生活场景。可以有杯子/吸管/纸巾/笔记本/手机等“手边物品”，但不要出现清晰人脸/完整人物。"
            if cat == "snack_food":
                return "产品是零食/食品，更适合追剧/加班/野餐/饭后的小场景。可以有零食碗/纸巾/遥控器/拆封包装等，但不要出现清晰人脸/完整人物。"
            if cat in {"skincare", "cosmetics"}:
                return "产品偏护肤/美妆，更适合浴室洗手台/梳妆台/包里随手拍的生活场景。可以有毛巾/化妆棉/镜子/化妆刷等，但不要出现清晰人脸/完整人物。"
            if cat == "home_cleaning":
                return "产品偏家清，更适合厨房/浴室/洗衣角落的家务场景。可以有抹布/水槽/洗衣篮/清洁工具等“正在用”的细节，但不要出现清晰人脸/完整人物。"
            if cat == "baby":
                return "产品偏母婴，更适合婴儿房/餐椅/妈咪包的生活场景。可以有收纳盒/奶瓶/小玩具等，但不要出现清晰人脸/完整人物。"
            if cat == "pet":
                return "产品偏宠物，更适合客厅地面/宠物窝旁的生活场景。可以有宠物碗/玩具/毛垫等，但不要出现清晰人脸/完整人物。"
            if cat == "electronics":
                return "产品偏数码，更适合书桌/床头/出行的生活场景。可以有线材/笔记本/台灯/行李标签等“人正在用”的细节，但不要出现清晰人脸/完整人物。"
            if cat == "fashion":
                return "产品偏穿搭，更适合玄关/衣柜/沙发角落的随手摆放场景。可以有衣架/镜子/包/钥匙等，但不要出现清晰人脸/完整人物。"
            if cat == "supplement":
                return "产品偏营养补充，更适合早餐桌/书桌/运动后角落的打卡场景。可以有水杯/勺子/毛巾等，但不要出现清晰人脸/完整人物。"
            return "尽量选择与产品用途更搭的生活场景与道具，不要棚拍摆拍。可以有“有人在场”的手边物品，但不要出现清晰人脸/完整人物。"

        # Shared prompt pieces
        glossy_prompt = (
            "Recreate this as a new Xiaohongshu-style post image. Keep the core subject and meaning, "
            "but change composition, background, color grading, lighting, and texture so it looks like a new post. "
            "No watermark, no extra text."
        )

        ugc_negative = (
            "studio lighting, commercial, ultra polished, beauty retouch, DSLR, bokeh, "
            "CGI, 3d render, perfect skin, over-sharpened, over-saturated, HDR, "
            "advertisement, e-commerce, hero shot, luxury, magazine, pristine minimal background, "
            "old, shabby, dirty, grime, mold, rust, broken, trash, messy clutter, "
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
        product_core_png_cache: dict[int, bytes] = {}
        input_ratio_cache: dict[int, float] = {}

        def _get_v2_assets(idx: int) -> tuple[bytes, bytes, bytes, bytes, float]:
            """Return (base_png, product_mask_png, edit_mask_png, product_core_png, input_ratio)."""
            with idx_locks[idx]:
                if idx in edit_mask_png_cache:
                    return (
                        base_png_cache[idx],
                        product_mask_png_cache[idx],
                        edit_mask_png_cache[idx],
                        product_core_png_cache[idx],
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

                # Build a cleaner "product core" mask for protection:
                # - use a high threshold to avoid including soft shadows/reflections
                # - apply morphological opening to remove small protrusions (often shadows)
                product_core = product_mask_l.point(lambda p: 255 if p >= core_threshold else 0, mode="L")
                if core_open_px > 0:
                    size = core_open_px * 2 + 1
                    if size >= 3:
                        product_core = product_core.filter(ImageFilter.MinFilter(size=size))
                        product_core = product_core.filter(ImageFilter.MaxFilter(size=size))

                # Optional additional erosion: biases towards keeping only the object interior,
                # so reflections/ground shadows are more likely to fall into the editable region.
                if core_erode_px > 0:
                    size = core_erode_px * 2 + 1
                    if size >= 3:
                        eroded = product_core.filter(ImageFilter.MinFilter(size=size))
                        if eroded.getbbox() is not None:
                            product_core = eroded

                # If threshold/opening is too strict, fall back to a lower threshold.
                if product_core.getbbox() is None:
                    product_core = product_mask_l.point(lambda p: 255 if p >= 160 else 0, mode="L")

                bbox = product_core.getbbox()
                input_ratio = bbox_dominance_ratio(bbox, size=product_mask_l.size)

                # In V2 we must be very conservative about keeping the product intact; otherwise
                # Painter may "rewrite" parts of the packaging and we later see a double/overlay.
                # Use a slightly looser threshold for the protected mask than the "core" mask,
                # so edges are protected while soft shadows/reflections (usually low alpha) stay editable.
                protect_threshold_default = max(160, core_threshold - 40)
                protect_threshold = int(os.getenv("AB_PRODUCT_PROTECT_THRESHOLD") or str(protect_threshold_default))
                protect_threshold = max(128, min(250, protect_threshold))
                product_protect = product_mask_l.point(lambda p: 255 if p >= protect_threshold else 0, mode="L")
                # Always include the core region (defensive against threshold edge-cases).
                try:
                    from PIL import ImageChops

                    product_protect = ImageChops.lighter(product_protect, product_core)
                except Exception:
                    pass

                protected = product_protect
                if protect_dilate_px > 0:
                    size = protect_dilate_px * 2 + 1
                    if size >= 3:
                        protected = protected.filter(ImageFilter.MaxFilter(size=size))

                # Painter convention: white=editable, black=protected.
                edit_mask_l = protected.point(lambda p: 255 - p, mode="L")
                edit_mask_png = mask_l_to_png_bytes(edit_mask_l)
                # Pasteback MUST be within the protected region; otherwise you create visible seams.
                product_core_png = mask_l_to_png_bytes(product_core)

                base_png_cache[idx] = base_png
                product_mask_png_cache[idx] = product_mask_png
                edit_mask_png_cache[idx] = edit_mask_png
                product_core_png_cache[idx] = product_core_png
                input_ratio_cache[idx] = input_ratio
                return base_png, product_mask_png, edit_mask_png, product_core_png, input_ratio

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
                    "整体更干净精修但仍真实自然：光源方向一致，透视正确，接触阴影自然，避免漂浮和贴图感。"
                    "去掉输入图里原本的硬阴影/倒影/镜面反射痕迹，重新生成与新背景一致的自然投影（如需要）。"
                    "不要水印，不要叠加文字/字幕/贴纸。主体在画面中的占比不要变大。"
                )
            if (style_preset or "ugc") == "glossy":
                return glossy_prompt

            scene = scene_tokens[idx % len(scene_tokens)]
            strength_hint = "中等变化" if lvl == "medium" else "更明显的变化"
            ugc_candid_hint = (
                "整体更像普通人手机随手拍：画面可以很干净、物品可以很新，但不要刻意摆拍。"
                "用拍摄手法与镜头感体现素人：轻微手持倾斜、构图不完美（不必居中对齐）、"
                "自然光/室内混合光、自动白平衡与曝光略有波动、局部阴影不过分柔美。"
                "可以出现“有人在场”的手边物品（手机/钥匙/纸巾/杯子/餐具/书本等），"
                "允许轻微噪点/压缩感/景深，但不要糊成一团。不要棚拍、不要电商主图质感。"
            )
            cat_hint = _category_hint(scene_category)

            if engine == "v2_mask":
                # Mask-edit mode: keep product stable; only rewrite background.
                return (
                    "基于输入图做『背景局部改写』：只改背景和道具，不要改变主体/产品本身（形状、大小比例、文字、logo尽量保持）。"
                    f"背景换成更真实生活感、不过分干净的场景（{scene}），{strength_hint}。"
                    f"{ugc_candid_hint}"
                    f"{cat_hint}"
                    "保持相机视角与透视一致，光源方向一致，透视正确，接触阴影自然，避免漂浮和贴图感。"
                    "去掉输入图里原本的硬阴影/倒影/镜面反射痕迹，重新生成与新背景一致的自然投影（如需要）。"
                    "不要新增大面积遮挡主体的物体；不要添加水印；不要叠加文字/字幕/贴纸。"
                    "主体在画面中的占比不要变大，尽量保持相同或略小。"
                )

            # V1: full img2img (less strict)
            return (
                f"把这张图改写成『真实生活感』风格：手机随手拍，生活场景（{scene}），{strength_hint}。"
                "不要棚拍、不要商业精修、不要过度锐化、不要HDR。"
                f"{ugc_candid_hint}"
                f"{cat_hint}"
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
            v2_edit_mask_png = None

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
                        base_png, product_mask_png, edit_mask_png, product_core_png, input_ratio = _get_v2_assets(idx)
                        product_mask_l = Image.open(io.BytesIO(product_mask_png)).convert("L")
                        # Use a stricter edit mask for aggressive to prevent bleed into the product.
                        edit_mask_png_use = edit_mask_png
                        if lvl == "aggressive":
                            try:
                                edit_l = Image.open(io.BytesIO(edit_mask_png)).convert("L")
                                extra = int(os.getenv("AB_V2_AGGRESSIVE_EDIT_ERODE_PX") or "2")
                                extra = max(0, min(32, extra))
                                if extra > 0:
                                    size = extra * 2 + 1
                                    if size >= 3:
                                        edit_l = edit_l.filter(ImageFilter.MinFilter(size=size))
                                edit_mask_png_use = mask_l_to_png_bytes(edit_l)
                            except Exception:
                                edit_mask_png_use = edit_mask_png

                        v2_edit_mask_png = edit_mask_png_use

                        # V2 uses mask-edit (background-only) instead of full img2img pasteback.
                        if lvl == "aggressive":
                            # Keep aggressive more stable to avoid editing bleeding into protected region.
                            g = 6.25
                            steps = 32
                            ps = 0.63
                        else:
                            g = 6.2
                            steps = 30
                            ps = 0.58

                        def _run_v2(ps_use: float) -> Image.Image:
                            out_bytes = painter.edit(
                                image_bytes=base_png,
                                mask_bytes=edit_mask_png_use,
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
                                    threshold=detail_threshold,
                                    inner_erode_px=detail_inner_erode_px,
                                )
                            return out_img

                        def _ratio_for(img: Image.Image) -> float | None:
                            try:
                                bufm2 = io.BytesIO()
                                img.convert("RGB").save(bufm2, format="PNG")
                                _, m2 = matting.matting(bufm2.getvalue(), filename=f"xhs_out_{idx+1:02d}.png")
                                bbox2 = bbox_from_mask_l(m2.convert("L"), threshold=core_threshold)
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
                                    # Record a lightweight warning for UI/debugging.
                                    r3 = _ratio_for(out3)
                                    if r3 is not None:
                                        err = f"scale_gate_fallback(in={input_ratio:.3f}, out={r2:.3f}, fallback={r3:.3f})"
                                    else:
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

                    # Pixel-fidelity pasteback (shared for V1/V2)
                    if (fidelity_mode or "pixel") == "pixel" and fg_rgba is not None and fg_mask is not None:
                        try:
                            from PIL import ImageChops

                            from app.services.harmonize import (
                                color_match_product,
                                despill,
                                edge_only_blend,
                                feather_alpha,
                            )
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

                            # Edge-only color harmonization: preserve opaque logo/text, but adjust edge pixels
                            # to better match the rewritten background.
                            try:
                                alpha_full = mask_feather.convert("L")
                                bbox = alpha_full.getbbox()
                                if bbox is not None:
                                    crop_rgb = fg2.convert("RGB").crop(bbox)
                                    crop_a = alpha_full.crop(bbox)
                                    matched = color_match_product(
                                        product_rgb=crop_rgb,
                                        product_alpha=crop_a,
                                        background_rgb=bg2,
                                        product_pos=(bbox[0], bbox[1]),
                                        match_strength=0.55,
                                    )
                                    edge_rgb = edge_only_blend(
                                        original_rgb=crop_rgb,
                                        adjusted_rgb=matched,
                                        alpha_l=crop_a,
                                        power=1.6,
                                    )
                                    fg2_rgb_full = fg2.convert("RGB")
                                    fg2_rgb_full.paste(edge_rgb, bbox)
                                    fg2 = Image.merge("RGBA", (*fg2_rgb_full.split(), alpha_full))
                            except Exception:
                                pass

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

            # Final UGC camera pass: apply globally so product/background share the same phone-like texture.
            if (style_preset or "ugc") == "ugc" and painter.configured and restyled_img is not img0:
                from app.services.ugc_degrade import apply_ugc_degrade

                j = lambda a, b: rng_local.uniform(a, b)
                noise = max(0.0, baseline["noise"] + j(-0.006, 0.010))
                restyled_img = apply_ugc_degrade(
                    restyled_img,
                    noise_strength=noise,
                    chroma_noise=max(0.0, noise * 0.55),
                    noise_shadow_boost=rng_local.uniform(0.55, 0.95),
                    contrast=min(0.96, max(0.65, baseline["contrast"] + j(-0.03, 0.03))),
                    saturation=min(1.05, max(0.65, baseline["saturation"] + j(-0.05, 0.05))),
                    sharpness=min(1.05, max(0.60, baseline["sharpness"] + j(-0.06, 0.05))),
                    blur_radius=min(1.20, max(0.18, baseline["blur"] + j(-0.10, 0.22))),
                    wb_shift=min(0.14, max(-0.12, baseline["wb"] + j(-0.03, 0.03))),
                    exposure=min(1.10, max(0.88, baseline["exposure"] + j(-0.03, 0.03))),
                    vignette=min(0.16, max(0.0, baseline["vignette"] + j(-0.02, 0.03))),
                    jpeg_quality=int(max(70, min(94, baseline["jpeg_q"] + int(j(-3, 3))))),
                    rotate_deg=j(-1.2, 1.2),
                )

                # Optional: a touch more background softness for depth-of-field, only in editable region (V2).
                if engine == "v2_mask" and isinstance(v2_edit_mask_png, (bytes, bytearray)):
                    try:
                        edit_l = Image.open(io.BytesIO(v2_edit_mask_png)).convert("L")
                        extra_blur = rng_local.uniform(0.20, 0.55) if lvl == "medium" else rng_local.uniform(0.28, 0.70)
                        bg_soft = restyled_img.filter(ImageFilter.GaussianBlur(radius=float(extra_blur)))
                        restyled_img = Image.composite(bg_soft, restyled_img, edit_l)
                    except Exception:
                        pass

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
