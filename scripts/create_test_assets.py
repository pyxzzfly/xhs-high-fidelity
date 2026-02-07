from pathlib import Path

from PIL import Image, ImageDraw


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "scripts" / "fixtures"

def create_test_assets():
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Create Product (Red Circle on Transparent)
    # This simulates a segmented product
    product = Image.new('RGBA', (512, 512), (0, 0, 0, 0))
    draw = ImageDraw.Draw(product)
    # Draw a red circle in the center
    draw.ellipse((156, 156, 356, 356), fill=(255, 0, 0, 255))
    out_prod = FIXTURES_DIR / "test_product.png"
    product.save(out_prod)
    print(f"Created {out_prod}")

    # 2. Create Reference (Simple Scene)
    # White background with a gray table surface
    reference = Image.new('RGB', (512, 512), (255, 255, 255))
    draw = ImageDraw.Draw(reference)
    # Draw table
    draw.rectangle((0, 300, 512, 512), fill=(200, 200, 200))
    # Draw a placeholder for where the product might be (just for structure)
    draw.rectangle((200, 250, 312, 400), outline=(100, 100, 100), width=2)
    out_ref = FIXTURES_DIR / "test_reference.png"
    reference.save(out_ref)
    print(f"Created {out_ref}")

if __name__ == "__main__":
    create_test_assets()
