import base64
import io
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple, cast

from PIL import Image

from app.services.prompts_loader import PromptsLoader
from app.core.logger import TaskLogger
from app.services.vision import VisionService


def _image_data_url_jpeg(image_b64: str) -> str:
    return f"data:image/jpeg;base64,{image_b64}"


def _encode_jpeg_b64(img: Image.Image, quality: int = 92) -> str:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def extract_scene_type(scene_desc: str) -> str:
    if not scene_desc:
        return "OTHER"
    for raw_line in scene_desc.strip().split("\n"):
        line = raw_line.strip()
        if line.startswith("SCENE_TYPE:") or line.startswith("SCENE TYPE:"):
            value = line.split(":", 1)[1].strip().upper()
            return value if value else "OTHER"
    return "OTHER"


def _contains_interaction_cues(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    keywords = [
        "hand",
        "hold",
        "holding",
        "grip",
        "finger",
        "palm",
        "in hand",
        "hands",
        "拿",
        "握",
        "手持",
        "手握",
        "手拿",
        "手部",
    ]
    return any(keyword in lowered for keyword in keywords)


def infer_scene_group(scene_type: str, layout_info: Optional[Dict[str, Any]] = None, scene_desc: str = "") -> str:
    normalized_type = (scene_type or "").strip().upper()
    info = layout_info or {}
    layout_type = str(info.get("layout_type", "")).strip().upper()
    slots = info.get("slots", [])
    slot_texts: List[str] = []
    if isinstance(slots, list):
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            slot_texts.append(str(slot.get("content", "")))
            slot_texts.append(str(slot.get("role", "")))
    joined_slots = " ".join(slot_texts)
    has_interaction = _contains_interaction_cues(scene_desc) or _contains_interaction_cues(joined_slots)

    if normalized_type == "HAND_HELD" or has_interaction:
        return "INTIMATE_INTERACTION"
    if normalized_type == "CLOSE_UP":
        return "DETAIL_FOCUS"
    if normalized_type == "FLAT_LAY":
        return "DISPLAY_FLAT"
    if normalized_type == "AMBIENT":
        return "AMBIENT_CONTEXT"

    if layout_type in {"GRID_COLLECTION", "SPLIT_COMPARISON"}:
        return "DISPLAY_FLAT"
    if layout_type in {"SINGLE_FOCUS", "MULTI_VIEW"}:
        return "DETAIL_FOCUS"
    return "AMBIENT_CONTEXT"


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _normalize_exaggeration_level(level: str) -> str:
    normalized = (level or "").strip()
    if normalized in {"轻微", "明显", "强烈"}:
        return normalized
    return "明显"


def get_scale_max() -> float:
    return max(0.1, min(_env_float("SCALE_MAX", 0.98), 1.0))


def get_dominance_band(scene_group: str, exaggeration_level: str) -> Tuple[float, float]:
    level = _normalize_exaggeration_level(exaggeration_level)
    level_key = {"轻微": "LIGHT", "明显": "MEDIUM", "强烈": "STRONG"}[level]
    group = (scene_group or "").strip().upper()
    if group not in {"INTIMATE_INTERACTION", "DETAIL_FOCUS", "DISPLAY_FLAT", "AMBIENT_CONTEXT"}:
        group = "DETAIL_FOCUS"

    defaults: Dict[str, Dict[str, Tuple[float, float]]] = {
        "LIGHT": {
            "INTIMATE_INTERACTION": (0.72, 0.88),
            "DETAIL_FOCUS": (0.62, 0.82),
            "DISPLAY_FLAT": (0.50, 0.75),
            "AMBIENT_CONTEXT": (0.30, 0.60),
        },
        "MEDIUM": {
            "INTIMATE_INTERACTION": (0.80, 0.94),
            "DETAIL_FOCUS": (0.68, 0.88),
            "DISPLAY_FLAT": (0.56, 0.80),
            "AMBIENT_CONTEXT": (0.35, 0.65),
        },
        "STRONG": {
            "INTIMATE_INTERACTION": (0.88, 0.98),
            "DETAIL_FOCUS": (0.78, 0.93),
            "DISPLAY_FLAT": (0.65, 0.88),
            "AMBIENT_CONTEXT": (0.40, 0.72),
        },
    }
    default_min, default_max = defaults[level_key][group]
    configured_min = _env_float(f"DOMINANCE_{group}_{level_key}_MIN", default_min)
    configured_max = _env_float(f"DOMINANCE_{group}_{level_key}_MAX", default_max)

    safe_min = max(0.1, min(configured_min, 1.0))
    safe_max = max(safe_min, min(configured_max, 1.0))
    return safe_min, safe_max


def clamp_scale_to_dominance(raw_scale: float, scene_group: str, exaggeration_level: str) -> Tuple[float, Tuple[float, float]]:
    lower, upper = get_dominance_band(scene_group, exaggeration_level)
    scale_max = get_scale_max()
    capped_upper = min(upper, scale_max)
    if capped_upper < lower:
        lower = capped_upper
    clamped = min(max(raw_scale, lower), capped_upper)
    return clamped, (lower, capped_upper)


class ReferenceAnalyzer:
    def __init__(self, vision: VisionService, prompts_dir: str):
        self.vision = vision
        self.prompts = PromptsLoader(prompts_dir)

    def _require_client(self):
        if getattr(self.vision, "client", None) is None:
            raise RuntimeError("Vision client not configured (BRAIN_API_KEY/BRAIN_BASE_URL)")
        return self.vision.client

    def analyze_scene_caption(self, ref_img: Image.Image, logger: TaskLogger) -> str:
        prompts = self.prompts.load()
        client = self._require_client()
        img_b64 = _encode_jpeg_b64(ref_img)
        resp = client.chat.completions.create(
            model=self.vision.model,
            temperature=0.1,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompts.scene_caption_prompt},
                        {"type": "image_url", "image_url": {"url": _image_data_url_jpeg(img_b64)}},
                    ],
                }
            ],
        )
        content = resp.choices[0].message.content
        text = cast(str, content) if isinstance(content, str) else ""
        return (text or "").strip()

    def parse_reference_layout(self, ref_img: Image.Image, logger: TaskLogger) -> Dict[str, Any]:
        prompts = self.prompts.load()
        client = self._require_client()
        img_b64 = _encode_jpeg_b64(ref_img)
        resp = client.chat.completions.create(
            model=self.vision.model,
            temperature=0.1,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompts.layout_parser_prompt},
                        {"type": "image_url", "image_url": {"url": _image_data_url_jpeg(img_b64)}},
                    ],
                }
            ],
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content
        text = cast(str, content) if isinstance(content, str) else ""
        data = _extract_json(text)
        if not data:
            raise RuntimeError("layout parser returned invalid json")
        # normalize
        data["layout_type"] = str(data.get("layout_type", "SINGLE_FOCUS")).upper()
        data["style"] = str(data.get("style", "COMMERCIAL")).upper()
        return data

    def analyze_product_scale(self, prod_rgba: Image.Image, ref_img: Image.Image, exaggeration_level: str, scene_desc: str, layout_info: Dict[str, Any], logger: TaskLogger) -> Dict[str, Any]:
        prompts = self.prompts.load()
        client = self._require_client()
        img_a = _encode_jpeg_b64(prod_rgba.convert("RGB"))
        img_b = _encode_jpeg_b64(ref_img)

        resp = client.chat.completions.create(
            model=self.vision.model,
            temperature=0.1,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompts.product_scale_prompt},
                        {"type": "image_url", "image_url": {"url": _image_data_url_jpeg(img_a)}},
                        {"type": "image_url", "image_url": {"url": _image_data_url_jpeg(img_b)}},
                    ],
                }
            ],
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content
        text = cast(str, content) if isinstance(content, str) else ""
        data = _extract_json(text) or {}

        raw_sf = 0.72
        try:
            sf = float(data.get("scale_factor", raw_sf))
            if 0.1 <= sf <= 1.0:
                raw_sf = sf
        except Exception:
            pass

        scene_type = extract_scene_type(scene_desc)
        scene_group = infer_scene_group(scene_type=scene_type, layout_info=layout_info, scene_desc=scene_desc)
        clamped_sf, band = clamp_scale_to_dominance(raw_sf, scene_group=scene_group, exaggeration_level=exaggeration_level)

        return {
            "raw_scale_factor": raw_sf,
            "clamped_scale_factor": clamped_sf,
            "scene_type": scene_type,
            "scene_group": scene_group,
            "target_dominance_band": [band[0], band[1]],
        }

    def pick_hero_position(self, layout_info: Dict[str, Any]) -> str:
        slots = layout_info.get("slots", [])
        if isinstance(slots, list):
            for slot in slots:
                if isinstance(slot, dict) and str(slot.get("role", "")).upper() == "HERO":
                    pos = str(slot.get("position", "center")).strip().lower()
                    return pos or "center"
        return "center"

    def analyze(self, prod_rgba: Image.Image, ref_img: Image.Image, exaggeration_level: str, logger: TaskLogger) -> Dict[str, Any]:
        # Fallback-only mode
        if getattr(self.vision, "client", None) is None:
            return {
                "mode": "fallback",
                "hero_position": "center",
                "scale": 0.72,
                "layout_info": {},
                "scene_desc": "",
                "scale_info": {},
            }

        started = time.perf_counter()
        scene_desc = self.analyze_scene_caption(ref_img, logger)
        layout_info = self.parse_reference_layout(ref_img, logger)
        scale_info = self.analyze_product_scale(
            prod_rgba=prod_rgba,
            ref_img=ref_img,
            exaggeration_level=exaggeration_level,
            scene_desc=scene_desc,
            layout_info=layout_info,
            logger=logger,
        )
        hero_position = self.pick_hero_position(layout_info)
        duration_ms = int((time.perf_counter() - started) * 1000)

        return {
            "mode": "model",
            "duration_ms": duration_ms,
            "hero_position": hero_position,
            "scale": float(scale_info.get("clamped_scale_factor", 0.72) or 0.72),
            "layout_info": layout_info,
            "scene_desc": scene_desc,
            "scale_info": scale_info,
        }
