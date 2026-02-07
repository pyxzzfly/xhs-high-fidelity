from __future__ import annotations

import os
from typing import Optional

import requests


class PainterClient:
    def __init__(self):
        self.edit_url = (os.getenv("PAINTER_EDIT_URL") or "").strip()
        self.token = (os.getenv("PAINTER_TOKEN") or "").strip()
        self.model = (os.getenv("PAINTER_MODEL") or "google/nano-banana").strip()

    @property
    def configured(self) -> bool:
        return bool(self.edit_url and self.token)

    def edit(
        self,
        *,
        image_bytes: bytes,
        prompt: str,
        negative_prompt: str = "",
        guidance_scale: float = 6.5,
        num_inference_steps: int = 28,
        prompt_strength: float = 0.65,
        output_format: str = "png",
        size: str | None = None,
        mask_bytes: bytes | None = None,
        timeout: int = 300,
    ) -> bytes:
        """Call Painter edit API.

        If mask_bytes is provided, we do a background/region edit (multipart: image+mask).
        """
        if not self.configured:
            raise RuntimeError("Painter not configured (need PAINTER_EDIT_URL + PAINTER_TOKEN)")

        safe_negative = negative_prompt.strip() if negative_prompt else ""
        if not safe_negative:
            safe_negative = (
                "watermark, text overlay, subtitles, low quality, blurry, ugly, deformed, "
                "extra limbs, bad anatomy"
            )

        headers = {"Authorization": f"Bearer {self.token}"}
        files = {"image": ("input.png", image_bytes, "image/png")}
        if mask_bytes is not None:
            files["mask"] = ("mask.png", mask_bytes, "image/png")

        data = {
            "model": self.model,
            "prompt": prompt,
            "negative_prompt": safe_negative,
            "guidance_scale": guidance_scale,
            "num_inference_steps": num_inference_steps,
            "prompt_strength": prompt_strength,
            "output_format": output_format,
        }
        if size:
            data["size"] = size

        max_attempts = int(os.getenv("PAINTER_RETRY_ATTEMPTS") or "3")
        retryable = {429, 500, 502, 503, 504, 520}
        last_exc: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                resp = requests.post(
                    self.edit_url,
                    headers=headers,
                    files=files,
                    data=data,
                    timeout=timeout,
                )
                if resp.ok:
                    break

                if resp.status_code in retryable and attempt < max_attempts:
                    # exponential backoff with small jitter
                    import random
                    import time

                    sleep_s = min(12.0, (0.8 * attempt) + random.uniform(0.0, 0.6 * attempt))
                    time.sleep(sleep_s)
                    continue

                raise RuntimeError(f"Painter edit failed status={resp.status_code} body={resp.text}")
            except Exception as exc:
                last_exc = exc
                if attempt >= max_attempts:
                    raise
                import random
                import time

                sleep_s = min(12.0, (0.8 * attempt) + random.uniform(0.0, 0.6 * attempt))
                time.sleep(sleep_s)

        else:
            if last_exc is not None:
                raise last_exc

        payload = resp.json()
        output_item: Optional[str] = None

        if isinstance(payload, dict):
            if isinstance(payload.get("output"), list) and payload["output"]:
                output_item = payload["output"][0]
            elif isinstance(payload.get("data"), list) and payload["data"]:
                data0 = payload["data"][0]
                if isinstance(data0, dict):
                    for k in ("b64", "b64_json", "url"):
                        if isinstance(data0.get(k), str):
                            output_item = data0[k]
                            break
                elif isinstance(data0, str):
                    output_item = data0
            elif isinstance(payload.get("url"), str):
                output_item = payload["url"]
        elif isinstance(payload, list) and payload:
            data0 = payload[0]
            if isinstance(data0, dict):
                for k in ("b64", "b64_json", "url"):
                    if isinstance(data0.get(k), str):
                        output_item = data0[k]
                        break
            elif isinstance(data0, str):
                output_item = data0

        if not isinstance(output_item, str) or not output_item:
            raise RuntimeError("Painter edit missing output")

        if output_item.startswith("http://") or output_item.startswith("https://"):
            r2 = requests.get(output_item, timeout=60)
            r2.raise_for_status()
            return r2.content
        if output_item.startswith("data:image"):
            import base64

            b64 = output_item.split(",", 1)[-1]
            return base64.b64decode(b64)

        import base64

        return base64.b64decode(output_item)

    def img2img(
        self,
        *,
        image_bytes: bytes,
        prompt: str,
        negative_prompt: str = "",
        guidance_scale: float = 6.5,
        num_inference_steps: int = 28,
        prompt_strength: float = 0.65,
        output_format: str = "png",
        timeout: int = 300,
    ) -> bytes:
        return self.edit(
            image_bytes=image_bytes,
            prompt=prompt,
            negative_prompt=negative_prompt,
            guidance_scale=guidance_scale,
            num_inference_steps=num_inference_steps,
            prompt_strength=prompt_strength,
            output_format=output_format,
            timeout=timeout,
            size=None,
            mask_bytes=None,
        )
