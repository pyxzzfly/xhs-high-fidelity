from PIL import Image, ImageFilter
import numpy as np

class ShadowService:
    def create_drop_shadow(
        self,
        mask: Image.Image,
        offset: tuple[int, int] = (20, 20),
        blur_radius: int = 15,
        opacity: float = 0.6,
        grow: int = 0,
    ) -> Image.Image:
        """
        Create a realistic drop shadow.
        Args:
            mask: Binary mask of the product (L mode)
            offset: (x, y) pixel offset
            blur_radius: Gaussian blur radius
            opacity: Shadow opacity (0.0 - 1.0)
        Returns:
            Image: Grayscale mask where 0=transparent, 255=opaque shadow
        """
        if mask.mode != "L":
            mask = mask.convert("L")
            
        width, height = mask.size
        # Create canvas for shadow
        shadow = Image.new("L", (width, height), 0)
        
        # Paste mask with offset
        shadow.paste(mask, offset)
        
        # Grow
        if grow > 0:
            shadow = shadow.filter(ImageFilter.MaxFilter(grow * 2 + 1))
            
        # Blur
        shadow = shadow.filter(ImageFilter.GaussianBlur(blur_radius))
        
        # Apply Opacity
        # We want the output to be a mask where 255 is "full shadow opacity"
        # If user requests 0.6 opacity, the max value should be 255 * 0.6 = 153
        if opacity < 1.0:
            shadow_np = np.array(shadow).astype(float)
            shadow_np = shadow_np * opacity
            shadow = Image.fromarray(shadow_np.astype("uint8"))
            
        return shadow

    def create_contact_shadow(
        self,
        placed_mask: Image.Image,
        band_ratio: float = 0.12,
        blur_radius: int = 8,
        opacity: float = 0.55,
        y_offset: int = 2,
    ) -> Image.Image:
        """Create a short, darker contact shadow near the product bottom edge.

        This specifically improves the 'bottom contact' realism, which is where sticker-look
        is most noticeable.

        Args:
            placed_mask: product alpha mask already placed on background canvas (L mode)
        """
        if placed_mask.mode != "L":
            placed_mask = placed_mask.convert("L")

        w, h = placed_mask.size
        m = np.array(placed_mask).astype(np.float32) / 255.0
        ys, xs = np.where(m > 0.2)
        if len(xs) < 50:
            return Image.new("L", (w, h), 0)

        y2 = int(ys.max())
        band_h = max(2, int((y2 - int(ys.min()) + 1) * band_ratio))
        y1 = max(0, y2 - band_h)

        band = np.zeros((h, w), dtype=np.float32)
        band[y1 : y2 + 1, :] = m[y1 : y2 + 1, :]

        # emphasize only the lower edge; small downward offset
        if y_offset != 0:
            shifted = np.zeros_like(band)
            if y_offset > 0:
                shifted[y_offset:, :] = band[: h - y_offset, :]
            else:
                shifted[: h + y_offset, :] = band[-y_offset:, :]
            band = shifted

        # blur and scale
        img = Image.fromarray(np.clip(band * 255, 0, 255).astype(np.uint8), mode="L")
        img = img.filter(ImageFilter.GaussianBlur(blur_radius))
        if opacity < 1.0:
            arr = np.array(img).astype(np.float32) * float(opacity)
            img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), mode="L")
        return img

    def create_perspective_shadow(self, mask: Image.Image, **kwargs) -> Image.Image:
        # TODO: Implement affine transform for V2
        # For now, fallback to drop shadow
        return self.create_drop_shadow(mask, **kwargs)
