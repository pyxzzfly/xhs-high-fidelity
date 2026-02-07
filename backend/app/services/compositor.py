from PIL import Image, ImageChops

class Compositor:
    @staticmethod
    def blend_layers(
        background: Image.Image,
        product_rgba: Image.Image,
        shadow_mask: Image.Image,
        product_pos: tuple[int, int]
    ) -> Image.Image:
        """
        Compose layers: Background * Shadow + Product
        
        Args:
            background: RGB image (Base)
            product_rgba: RGBA image (Top)
            shadow_mask: L mode image (0=transparent, 255=max shadow darkness)
            product_pos: (x, y) where product is placed
        """
        bg = background.convert("RGBA")
        w, h = bg.size
        
        # 1. Prepare Shadow Multiplier Layer
        # We need a layer that is White (255) where there is NO shadow, 
        # and Darker (<255) where there IS shadow.
        # shadow_mask has 0=no shadow, 255=max shadow.
        # So we Invert it: 0->255 (White), 255->0 (Black).
        shadow_multiplier = ImageChops.invert(shadow_mask)
        
        # Resize if needed
        if shadow_multiplier.size != (w, h):
            shadow_multiplier = shadow_multiplier.resize((w, h))
            
        # 2. Apply Shadow (Multiply)
        # Result = Background * Multiplier
        bg_rgb = bg.convert("RGB")
        shadow_rgb = shadow_multiplier.convert("RGB")
        
        shadowed_bg = ImageChops.multiply(bg_rgb, shadow_rgb)
        shadowed_bg = shadowed_bg.convert("RGBA")
        
        # 3. Paste Product (Alpha Composite)
        # Create transparent layer for product
        product_layer = Image.new("RGBA", (w, h), (0,0,0,0))
        product_layer.paste(product_rgba, product_pos)
        
        # Alpha Composite: Put product OVER shadowed background
        final_image = Image.alpha_composite(shadowed_bg, product_layer)
        
        return final_image.convert("RGB")
