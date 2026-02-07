import requests
from PIL import Image
import io
import base64
import os
from pathlib import Path

API_URL = "http://localhost:8000/api/v1"
REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "scripts" / "fixtures"
ARTIFACTS_DIR = REPO_ROOT / "artifacts" / "poc_output"

def test_full_flow():
    # Ensure test assets exist
    prod_path = FIXTURES_DIR / "test_product.png"
    ref_path = FIXTURES_DIR / "test_reference.png"
    if not prod_path.exists() or not ref_path.exists():
        print("Please run scripts/create_test_assets.py first (it will generate scripts/fixtures/*.png)")
        return

    # 1. Generate Image
    print(">> Testing Image Generation (Vision Analysis + Inpainting + Shadow)...")
    try:
        files = {
            "product_image": ("prod.png", open(prod_path, "rb"), "image/png"),
            "reference_image": ("ref.png", open(ref_path, "rb"), "image/png")
        }
        # Note: We are NOT sending a prompt. We expect the backend to infer it via VisionService.
        
        resp = requests.post(f"{API_URL}/generate", files=files, timeout=120)
        
        if resp.status_code == 200:
            data = resp.json()
            print(f"✅ Image Success! Task ID: {data.get('task_id')}")
            analysis = data.get('scene_analysis', {})
            print(f"   Inferred Prompt: {analysis.get('scene_description', 'N/A')}")
            print(f"   Lighting: {analysis.get('lighting_direction', 'N/A')}")
            
            # Save output
            if 'image_base64' in data:
                img_data = base64.b64decode(data['image_base64'])
                ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
                out = ARTIFACTS_DIR / "integration_result.png"
                with open(out, "wb") as f:
                    f.write(img_data)
                print(f"   Saved to {out}")
        else:
            print(f"❌ Image Gen Failed: {resp.status_code} - {resp.text}")
    except Exception as e:
        print(f"❌ Connection Failed: {e}")

    # 2. Generate Copy
    print("\n>> Testing Copy Generation (Gemini 3 Pro)...")
    try:
        data = {
            "product_name": "Magic Perfume",
            "features": "Long lasting, floral scent, premium bottle",
            "reference_text": "Check out this amazing perfume! #summer #vibes"
        }
        resp = requests.post(f"{API_URL}/generate_copy", data=data, timeout=30)
        
        if resp.status_code == 200:
            copy = resp.json()
            print(f"✅ Copy Success!")
            print(f"   Title: {copy.get('title')}")
            print(f"   Content Snippet: {copy.get('content')[:50]}...")
        else:
            print(f"❌ Copy Gen Failed: {resp.status_code} - {resp.text}")
    except Exception as e:
        print(f"❌ Connection Failed: {e}")

if __name__ == "__main__":
    test_full_flow()
