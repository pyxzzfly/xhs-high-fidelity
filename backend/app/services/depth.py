from transformers import DPTForDepthEstimation, DPTImageProcessor
from PIL import Image
import torch
import numpy as np

class DepthService:
    def __init__(self, device="cpu"):
        self.device = device
        self.processor = None
        self.model = None

    def _load_model(self):
        if self.model is None:
            print(f"Loading Depth Model on {self.device}...")
            self.processor = DPTImageProcessor.from_pretrained("Intel/dpt-large")
            self.model = DPTForDepthEstimation.from_pretrained("Intel/dpt-large")
            if self.device != "cpu":
                self.model.to(self.device)
            self.model.eval()
            print("Depth Model Loaded.")

    def extract_depth_map(self, image: Image.Image) -> Image.Image:
        """
        Extract depth map from image.
        Returns grayscale image (0-255).
        """
        self._load_model()
        
        # Ensure RGB
        if image.mode != "RGB":
            image = image.convert("RGB")

        inputs = self.processor(images=image, return_tensors="pt")
        if self.device != "cpu":
            inputs = {k: v.to(self.device) for k, v in inputs.items()}
            
        with torch.no_grad():
            outputs = self.model(**inputs)
            predicted_depth = outputs.predicted_depth
            
        # Interpolate to original size
        prediction = torch.nn.functional.interpolate(
            predicted_depth.unsqueeze(1),
            size=image.size[::-1],
            mode="bicubic",
            align_corners=False,
        )
        
        output = prediction.squeeze().cpu().numpy()
        
        # Normalize
        depth_min = output.min()
        depth_max = output.max()
        
        if depth_max - depth_min > 1e-6:
            normalized = (output - depth_min) / (depth_max - depth_min)
        else:
            normalized = np.zeros_like(output)
            
        formatted = (normalized * 255).astype("uint8")
        return Image.fromarray(formatted)
