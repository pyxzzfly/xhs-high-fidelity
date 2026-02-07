from __future__ import annotations

import json
import os
from typing import Any, Dict

from app.services.vision import VisionService


def _count_words_rough(text: str) -> int:
    # Chinese: count characters excluding spaces; fallback for mixed text
    if not text:
        return 0
    t = "".join(ch for ch in text if not ch.isspace())
    return len(t)


class RewriteService:
    def __init__(self, vision: VisionService, prompt_path: str):
        self.vision = vision
        self.prompt_path = prompt_path

    def _load_prompt(self) -> str:
        with open(self.prompt_path, "r", encoding="utf-8") as f:
            return f.read().strip()

    def rewrite_one(
        self,
        *,
        template_id: str,
        product_name: str,
        product_features: str,
        original_text: str,
    ) -> Dict[str, Any]:
        if not original_text.strip():
            raise ValueError("original_text required")
        # product_name/features can be empty for XHS link rewrite; model should infer from original.
        product_name = (product_name or "").strip()
        product_features = (product_features or "").strip()
        if not product_name:
            product_name = "(从原稿中自动识别)"

        if getattr(self.vision, "client", None) is None:
            raise RuntimeError(
                "Vision/LLM client not configured. Set BRAIN_API_KEY and BRAIN_BASE_URL for rewrite."  # noqa
            )

        sys_prompt = self._load_prompt()
        target_wc = _count_words_rough(original_text)

        user = {
            "TEMPLATE_ID": template_id,
            "PRODUCT_NAME": product_name,
            "PRODUCT_FEATURES": product_features,
            "ORIGINAL_TEXT": original_text,
            "TARGET_WORD_COUNT": target_wc,
        }

        resp = self.vision.client.chat.completions.create(
            model=self.vision.model,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
            ],
            response_format={"type": "json_object"},
            temperature=0.7,
        )

        content = resp.choices[0].message.content
        data = json.loads(content) if isinstance(content, str) else {}
        if not isinstance(data, dict):
            data = {}
        # best-effort word_count
        if not data.get("word_count") and isinstance(data.get("content"), str):
            data["word_count"] = _count_words_rough(data["content"])
        data["target_word_count"] = target_wc
        data["template_id"] = template_id
        return data
