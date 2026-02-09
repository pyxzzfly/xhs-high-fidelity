import os
import json
import logging
from PIL import Image
import base64
import io
from openai import OpenAI # Compatible client for Gemini
logger = logging.getLogger("xhs-high-fidelity")

class VisionService:
    def __init__(self):
        self.api_key = os.getenv("BRAIN_API_KEY")
        self.base_url = os.getenv("BRAIN_BASE_URL")
        # Text-only and multimodal both use the same OpenAI-compatible client;
        # whether images work depends on the selected model.
        self.model = os.getenv("BRAIN_MODEL") or "gemini-3-pro"

        enforce = (os.getenv("ENFORCE_GOOGLE_MODELS") or "").strip().lower() in {"1", "true", "yes", "on"}
        if enforce and self.model and "gemini" not in self.model.lower():
            raise RuntimeError(f"BRAIN_MODEL must be a Gemini model id (got: {self.model})")

        self.client = None
        if not self.api_key or not self.base_url:
            logger.warning(
                "BRAIN client not configured (need BRAIN_API_KEY + BRAIN_BASE_URL). "
                "AI features will run in fallback mode."
            )
        else:
            self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)

    def _encode_image(self, image: Image.Image) -> str:
        buf = io.BytesIO()
        image.save(buf, format="JPEG")
        return base64.b64encode(buf.getvalue()).decode('utf-8')

    def analyze_reference(self, image: Image.Image) -> dict:
        """
        Analyze reference image for layout, lighting, and scene context.
        """
        img_b64 = self._encode_image(image)
        
        prompt = """
        Analyze this image for a product photography composite. 
        Output JSON with:
        {
            "lighting_direction": "top-left" | "top-right" | "soft-ambient",
            "scene_description": "Detailed description of background and props...",
            "composition": "center" | "rule-of-thirds",
            "depth_structure": "foreground table, blurred background",
            "product_placement_suggestion": "where the product should sit"
        }
        """
        
        try:
            if self.client is None:
                raise RuntimeError("Vision client not configured")
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                            },
                        ],
                    }
                ],
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content
            return json.loads(content)
        except Exception as e:
            logger.error(f"Vision Analysis Failed: {e}")
            # Fallback
            return {
                "lighting_direction": "soft-ambient",
                "scene_description": "A clean, high-quality product background",
                "composition": "center"
            }

    def generate_xhs_copy(self, product_name: str, features: str, reference_copy: str = "") -> dict:
        """
        Generate Xiaohongshu style copy.
        """
        prompt = f"""
        你是小红书爆款文案专家。请根据以下信息写一篇推文。
        
        产品名称: {product_name}
        产品卖点: {features}
        参考风格: {reference_copy}
        
        要求:
        1. 标题吸引人，带Emoji。
        2. 正文分段清晰，口语化，多用Emoji。
        3. 包含热门标签。
        
        返回 JSON:
        {{
            "title": "...",
            "content": "..."
        }}
        """
        
        try:
            if self.client is None:
                raise RuntimeError("Vision client not configured")
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            logger.error(f"Copy Generation Failed: {e}")
            return {"title": "Error", "content": "Failed to generate copy."}
