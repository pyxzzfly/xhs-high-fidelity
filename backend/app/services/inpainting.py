import os
import requests
import base64
import io
import json
import logging
from PIL import Image
logger = logging.getLogger("xhs-high-fidelity")

class InpaintingService:
    def __init__(self, device="cpu"):
        self.api_url = os.getenv("PAINTER_EDIT_URL", "http://localhost:3000/replicate/images/edits")
        self.api_token = os.getenv("PAINTER_TOKEN", "")
        # Keep aligned with PainterClient; override via PAINTER_MODEL.
        self.model = (os.getenv("PAINTER_MODEL") or "google/nano-banana").strip()
        enforce = (os.getenv("ENFORCE_GOOGLE_MODELS") or "").strip().lower() in {"1", "true", "yes", "on"}
        if enforce and self.model and not self.model.lower().startswith("google/"):
            raise RuntimeError(f"PAINTER_MODEL must be a Google model id (got: {self.model})")
        
    def generate_background(
        self,
        prompt: str,
        image: Image.Image,
        mask_image: Image.Image,
        control_image: Image.Image = None,
        num_inference_steps: int = 30,
        guidance_scale: float = 7.5,
        controlnet_conditioning_scale: float = 0.5,
        strength: float = 0.99
    ) -> Image.Image:
        """
        Generate background using Google Banana Pro via API (Multipart Upload).
        """
        # Prepare Image Bytes
        img_byte_arr = io.BytesIO()
        image.save(img_byte_arr, format='PNG')
        img_bytes = img_byte_arr.getvalue()
        
        mask_byte_arr = io.BytesIO()
        mask_image.save(mask_byte_arr, format='PNG')
        mask_bytes = mask_byte_arr.getvalue()

        headers = {
            "Authorization": f"Bearer {self.api_token}"
        }
        
        # Multipart form data
        files = {
            "image": ("init_image.png", img_bytes, "image/png"),
            "mask": ("mask.png", mask_bytes, "image/png")
        }
        
        data = {
            "model": self.model,
            "prompt": prompt,
            "num_inference_steps": str(num_inference_steps),
            "guidance_scale": str(guidance_scale),
            "prompt_strength": str(strength),
            "output_format": "png",
            "negative_prompt": "text, watermark, low quality, blurry, ugly, deformed"
        }
        
        # Optional: ControlNet Input (if API supports it)
        if control_image:
             # Resize control image to match input
             control_image = control_image.resize(image.size)
             ctrl_byte_arr = io.BytesIO()
             control_image.save(ctrl_byte_arr, format='PNG')
             files["control_image"] = ("control.png", ctrl_byte_arr.getvalue(), "image/png")

        logger.info(f"Calling Painter API with model {self.model}...")
        
        try:
            response = requests.post(self.api_url, headers=headers, files=files, data=data, timeout=120)
            
            if not response.ok:
                logger.error(f"API Error: {response.status_code} - {response.text}")
                response.raise_for_status()
            
            result = response.json()
            
            # Handle output parsing (robust to various formats)
            output_url = None
            if isinstance(result, list) and len(result) > 0:
                output_url = result[0]
            elif isinstance(result, dict):
                if "output" in result:
                    out = result["output"]
                    if isinstance(out, list) and len(out) > 0:
                        output_url = out[0]
                    elif isinstance(out, str):
                        output_url = out
                elif "url" in result:
                    output_url = result["url"]
            
            if not output_url:
                # Check for direct base64 data in custom fields
                if "data" in result and isinstance(result["data"], list):
                    item = result["data"][0]
                    if isinstance(item, dict):
                         if "b64" in item:
                             output_url = item["b64"]
                         elif "url" in item:
                             output_url = item["url"]
            
            if not output_url:
                raise ValueError(f"Invalid API response format: {result}")
                
            # Download result
            if output_url.startswith("http"):
                img_data = requests.get(output_url).content
            else:
                # Assume base64
                if "," in output_url:
                    output_url = output_url.split(",", 1)[1]
                img_data = base64.b64decode(output_url)
                
            return Image.open(io.BytesIO(img_data)).convert("RGB")
            
        except Exception as e:
            logger.error(f"Inpainting API Failed: {str(e)}")
            raise e
