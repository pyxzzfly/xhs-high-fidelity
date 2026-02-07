import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class PromptCache:
    scene_caption_prompt: str
    layout_parser_prompt: str
    product_scale_prompt: str
    negative_prompt: str
    scene_caption_mtime: float
    layout_parser_mtime: float
    product_scale_mtime: float
    negative_prompt_mtime: float


class PromptsLoader:
    def __init__(self, base_dir: str) -> None:
        self.base_dir = base_dir
        self._cache: Optional[PromptCache] = None

    def _read_file(self, path: str) -> str:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()

    def load(self) -> PromptCache:
        scene_caption_path = os.path.join(self.base_dir, "scene_caption.txt")
        layout_parser_path = os.path.join(self.base_dir, "layout_parser.txt")
        product_scale_path = os.path.join(self.base_dir, "product_scale.txt")
        negative_prompt_path = os.path.join(self.base_dir, "negative_prompt.txt")

        scene_caption_mtime = os.path.getmtime(scene_caption_path)
        layout_parser_mtime = os.path.getmtime(layout_parser_path)
        product_scale_mtime = os.path.getmtime(product_scale_path)
        negative_prompt_mtime = os.path.getmtime(negative_prompt_path)

        if self._cache:
            if (
                self._cache.scene_caption_mtime == scene_caption_mtime
                and self._cache.layout_parser_mtime == layout_parser_mtime
                and self._cache.product_scale_mtime == product_scale_mtime
                and self._cache.negative_prompt_mtime == negative_prompt_mtime
            ):
                return self._cache

        self._cache = PromptCache(
            scene_caption_prompt=self._read_file(scene_caption_path),
            layout_parser_prompt=self._read_file(layout_parser_path),
            product_scale_prompt=self._read_file(product_scale_path),
            negative_prompt=self._read_file(negative_prompt_path),
            scene_caption_mtime=scene_caption_mtime,
            layout_parser_mtime=layout_parser_mtime,
            product_scale_mtime=product_scale_mtime,
            negative_prompt_mtime=negative_prompt_mtime,
        )
        return self._cache
