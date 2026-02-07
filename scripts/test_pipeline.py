from PIL import Image, ImageOps
import sys
import os
from pathlib import Path

# Add backend to path
sys.path.append(os.path.join(os.path.dirname(__file__), '../backend'))

from app.services.shadow import ShadowService
from app.services.compositor import Compositor

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "scripts" / "fixtures"
ARTIFACTS_DIR = REPO_ROOT / "artifacts" / "poc_output"

def test_pipeline():
    print("Testing Pipeline Components...")
    
    # 1. Load Assets
    try:
        product = Image.open(FIXTURES_DIR / 'test_product.png').convert("RGBA")
        background = Image.open(FIXTURES_DIR / 'test_reference.png').convert("RGB") # Use reference as BG for now
        print(f"Loaded assets. Product: {product.size}, BG: {background.size}")
    except FileNotFoundError:
        print("Error: Test assets not found. Run scripts/create_test_assets.py first.")
        return

    # 2. Setup Services
    shadow_svc = ShadowService()
    compositor = Compositor()
    
    # 3. Simulate Layout (Center product)
    canvas_w, canvas_h = background.size
    # Scale product to 50% height
    scale = (canvas_h * 0.5) / product.height
    new_w, new_h = int(product.width * scale), int(product.height * scale)
    product_resized = product.resize((new_w, new_h))
    
    pos_x = (canvas_w - new_w) // 2
    pos_y = (canvas_h - new_h) // 2
    print(f"Product placement: ({pos_x}, {pos_y}), Size: {new_w}x{new_h}")
    
    # 4. Generate Shadow
    # Extract mask from resized product alpha
    product_mask = product_resized.split()[3]
    
    # Create full-size mask for shadow
    full_mask = Image.new("L", (canvas_w, canvas_h), 0)
    full_mask.paste(product_mask, (pos_x, pos_y))
    
    print("Generating shadow...")
    shadow = shadow_svc.create_drop_shadow(
        mask=full_mask,
        offset=(20, 20),
        blur_radius=15,
        opacity=0.6
    )
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    shadow.save(ARTIFACTS_DIR / 'test_shadow_mask.png')
    
    # 5. Composite
    print("Compositing...")
    final = compositor.blend_layers(
        background=background,
        product_rgba=product_resized,
        shadow_mask=shadow,
        product_pos=(pos_x, pos_y)
    )
    
    # Save output
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    out = ARTIFACTS_DIR / 'test_final.png'
    final.save(out)
    print(f"Success! Output saved to {out}")

if __name__ == "__main__":
    test_pipeline()
