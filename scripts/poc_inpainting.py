#!/usr/bin/env python3
"""
PoC: 测试 Google Banana Pro 的 Inpainting 能力

目标：验证 Painter API 是否真正支持 inpainting（保留 mask 以外区域）
"""

import os
import sys
import base64
import requests
from pathlib import Path
from io import BytesIO
from datetime import datetime

# 尝试导入 PIL，如果没有则提示安装
try:
    from PIL import Image, ImageDraw
except ImportError:
    print("❌ 请先安装 Pillow: pip install Pillow")
    sys.exit(1)

# 尝试导入 dotenv
try:
    from dotenv import load_dotenv
except ImportError:
    print("❌ 请先安装 python-dotenv: pip install python-dotenv")
    sys.exit(1)


# ============ 配置区 ============

# 测试图片路径（可以替换为你自己的图片）
TEST_IMAGE_PATH = None  # 留空则自动生成测试图

# 输出目录（不污染 backend/，统一放 artifacts 下）
OUTPUT_DIR = Path(__file__).resolve().parents[1] / "artifacts" / "poc_output"


# ============ 工具函数 ============

def load_env():
    """加载 .env 配置"""
    # Load this repo's backend/.env
    env_path = Path(__file__).resolve().parents[1] / "backend" / ".env"
    if not env_path.exists():
        print(f"❌ 找不到 .env 文件: {env_path}")
        sys.exit(1)
    
    load_dotenv(env_path)
    
    painter_url = os.getenv("PAINTER_EDIT_URL")
    painter_token = os.getenv("PAINTER_TOKEN")
    
    if not painter_url or not painter_token:
        print("❌ 缺少 PAINTER_EDIT_URL 或 PAINTER_TOKEN")
        sys.exit(1)
    
    print(f"✅ 加载配置成功")
    print(f"   PAINTER_EDIT_URL: {painter_url}")
    print(f"   PAINTER_TOKEN: {painter_token[:20]}...")
    
    return painter_url, painter_token


def create_test_image(size=(512, 512)):
    """
    创建一张测试图：
    - 左半边：红色渐变
    - 右半边：蓝色渐变
    - 中心：绿色圆形
    
    这样可以清晰地看出 inpainting 是否保留了原图区域
    """
    img = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(img)
    
    w, h = size
    
    # 左半边红色渐变
    for x in range(w // 2):
        intensity = int(255 * x / (w // 2))
        for y in range(h):
            img.putpixel((x, y), (255, intensity, intensity))
    
    # 右半边蓝色渐变
    for x in range(w // 2, w):
        intensity = int(255 * (x - w // 2) / (w // 2))
        for y in range(h):
            img.putpixel((x, y), (intensity, intensity, 255))
    
    # 中心绿色圆形（这部分会被 mask 覆盖，应该被修改）
    center = (w // 2, h // 2)
    radius = min(w, h) // 6
    draw.ellipse(
        [center[0] - radius, center[1] - radius, 
         center[0] + radius, center[1] + radius],
        fill=(0, 255, 0)
    )
    
    # 四角加上标记文字（如果有字体的话）
    try:
        from PIL import ImageFont
        font = ImageFont.load_default()
        draw.text((10, 10), "TL", fill=(0, 0, 0), font=font)
        draw.text((w - 30, 10), "TR", fill=(0, 0, 0), font=font)
        draw.text((10, h - 20), "BL", fill=(0, 0, 0), font=font)
        draw.text((w - 30, h - 20), "BR", fill=(0, 0, 0), font=font)
    except:
        pass
    
    return img


def create_center_mask(size=(512, 512), mask_ratio=0.3):
    """
    创建中心矩形 mask
    - 白色区域：需要修改的部分
    - 黑色区域：需要保留的部分
    """
    mask = Image.new("L", size, 0)  # 全黑（保留）
    draw = ImageDraw.Draw(mask)
    
    w, h = size
    mask_w = int(w * mask_ratio)
    mask_h = int(h * mask_ratio)
    
    x1 = (w - mask_w) // 2
    y1 = (h - mask_h) // 2
    x2 = x1 + mask_w
    y2 = y1 + mask_h
    
    # 中心矩形区域标记为白色（需要修改）
    draw.rectangle([x1, y1, x2, y2], fill=255)
    
    return mask


def image_to_base64(img: Image.Image, format="PNG") -> str:
    """将 PIL Image 转为 base64 字符串"""
    buffer = BytesIO()
    img.save(buffer, format=format)
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


def base64_to_image(b64_str: str) -> Image.Image:
    """将 base64 字符串转为 PIL Image"""
    # 处理可能的 data URL 前缀
    if "," in b64_str:
        b64_str = b64_str.split(",", 1)[1]
    img_data = base64.b64decode(b64_str)
    return Image.open(BytesIO(img_data))


def call_painter_inpainting(
    url: str,
    token: str,
    image: Image.Image,
    mask: Image.Image,
    prompt: str = "a beautiful sunset sky",
    n: int = 1
) -> dict:
    """
    调用 Painter API 进行 inpainting
    
    尝试多种可能的请求格式
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    # 转换图片为 base64
    image_b64 = image_to_base64(image)
    mask_b64 = image_to_base64(mask)
    
    # 请求体 - 尝试标准的 OpenAI 风格 inpainting 格式
    payload = {
        "model": "imagen-3.0-capability-001",  # Google Imagen 3
        "prompt": prompt,
        "n": n,
        "size": f"{image.width}x{image.height}",
        # inpainting 关键字段
        "image": f"data:image/png;base64,{image_b64}",
        "mask": f"data:image/png;base64,{mask_b64}",
    }
    
    print(f"\n📤 发送 Inpainting 请求...")
    print(f"   URL: {url}")
    print(f"   Prompt: {prompt}")
    print(f"   Image size: {image.width}x{image.height}")
    print(f"   Payload keys: {list(payload.keys())}")
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=120)
        print(f"   Status: {response.status_code}")
        
        if response.status_code == 200:
            return {"success": True, "data": response.json()}
        else:
            return {
                "success": False, 
                "status": response.status_code,
                "error": response.text[:500]
            }
    except Exception as e:
        return {"success": False, "error": str(e)}


def call_painter_inpainting_multipart(
    url: str,
    token: str,
    image: Image.Image,
    mask: Image.Image,
    prompt: str = "a beautiful sunset sky",
) -> dict:
    """
    使用 multipart/form-data 格式调用（OpenAI 原生格式）
    """
    headers = {
        "Authorization": f"Bearer {token}",
    }
    
    # 准备文件
    image_buffer = BytesIO()
    image.save(image_buffer, format="PNG")
    image_buffer.seek(0)
    
    mask_buffer = BytesIO()
    mask.save(mask_buffer, format="PNG")
    mask_buffer.seek(0)
    
    files = {
        "image": ("image.png", image_buffer, "image/png"),
        "mask": ("mask.png", mask_buffer, "image/png"),
    }
    
    data = {
        "model": "imagen-3.0-capability-001",
        "prompt": prompt,
        "n": "1",
        "size": f"{image.width}x{image.height}",
    }
    
    print(f"\n📤 发送 Inpainting 请求 (multipart)...")
    print(f"   URL: {url}")
    print(f"   Prompt: {prompt}")
    
    try:
        response = requests.post(url, headers=headers, files=files, data=data, timeout=120)
        print(f"   Status: {response.status_code}")
        
        if response.status_code == 200:
            return {"success": True, "data": response.json()}
        else:
            return {
                "success": False, 
                "status": response.status_code,
                "error": response.text[:500]
            }
    except Exception as e:
        return {"success": False, "error": str(e)}


def analyze_result(original: Image.Image, mask: Image.Image, result: Image.Image) -> dict:
    """
    分析结果：检查 mask 外的区域是否被保留
    
    Returns:
        dict: 包含分析结果的字典
    """
    import numpy as np
    
    orig_arr = np.array(original)
    result_arr = np.array(result)
    mask_arr = np.array(mask)
    
    # mask == 0 的区域应该保留（黑色区域）
    preserved_mask = mask_arr == 0
    
    # 计算保留区域的像素差异
    if len(orig_arr.shape) == 3:
        preserved_mask_3d = np.stack([preserved_mask] * 3, axis=-1)
    else:
        preserved_mask_3d = preserved_mask
    
    preserved_orig = orig_arr[preserved_mask_3d]
    preserved_result = result_arr[preserved_mask_3d]
    
    # 平均像素差异
    mean_diff = np.mean(np.abs(preserved_orig.astype(float) - preserved_result.astype(float)))
    max_diff = np.max(np.abs(preserved_orig.astype(float) - preserved_result.astype(float)))
    
    # 完全相同的像素比例
    exact_match_ratio = np.mean(preserved_orig == preserved_result)
    
    # 判断是否真正保留
    is_preserved = mean_diff < 5.0  # 允许微小的压缩差异
    
    return {
        "mean_pixel_diff": float(mean_diff),
        "max_pixel_diff": float(max_diff),
        "exact_match_ratio": float(exact_match_ratio),
        "is_truly_inpainting": is_preserved,
        "verdict": "✅ 真正的 Inpainting" if is_preserved else "❌ 可能是全图重绘"
    }


def main():
    print("=" * 60)
    print("🎨 Google Banana Pro Inpainting PoC")
    print("=" * 60)
    
    # 1. 加载配置
    painter_url, painter_token = load_env()
    
    # 2. 准备输出目录
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # 3. 准备测试图片
    if TEST_IMAGE_PATH and Path(TEST_IMAGE_PATH).exists():
        print(f"\n📷 加载测试图片: {TEST_IMAGE_PATH}")
        original_image = Image.open(TEST_IMAGE_PATH).convert("RGB")
    else:
        print("\n📷 生成测试图片...")
        original_image = create_test_image(size=(512, 512))
    
    # 保存原图
    original_path = OUTPUT_DIR / f"{timestamp}_1_original.png"
    original_image.save(original_path)
    print(f"   保存原图: {original_path}")
    
    # 4. 生成 Mask
    print("\n🎭 生成 Mask（中心矩形区域）...")
    mask = create_center_mask(size=original_image.size, mask_ratio=0.3)
    
    mask_path = OUTPUT_DIR / f"{timestamp}_2_mask.png"
    mask.save(mask_path)
    print(f"   保存 Mask: {mask_path}")
    
    # 可视化 mask 叠加效果
    overlay = original_image.copy()
    overlay.paste((255, 0, 0), mask=mask)  # 红色标记 mask 区域
    overlay = Image.blend(original_image, overlay, 0.3)
    overlay_path = OUTPUT_DIR / f"{timestamp}_3_overlay.png"
    overlay.save(overlay_path)
    print(f"   保存叠加预览: {overlay_path}")
    
    # 5. 调用 API - 尝试 JSON 格式
    print("\n" + "=" * 40)
    print("📡 测试 1: JSON 格式请求")
    print("=" * 40)
    
    result1 = call_painter_inpainting(
        url=painter_url,
        token=painter_token,
        image=original_image,
        mask=mask,
        prompt="a bright yellow sun in a clear blue sky"
    )
    
    if result1["success"]:
        print("✅ JSON 格式请求成功!")
        print(f"   响应数据 keys: {list(result1['data'].keys())}")
        
        # 尝试解析返回的图片
        try:
            data = result1["data"]
            if "data" in data and len(data["data"]) > 0:
                img_data = data["data"][0]
                if "b64_json" in img_data:
                    result_image = base64_to_image(img_data["b64_json"])
                elif "url" in img_data:
                    print(f"   返回了 URL: {img_data['url']}")
                    # 下载图片
                    resp = requests.get(img_data["url"])
                    result_image = Image.open(BytesIO(resp.content))
                else:
                    result_image = None
                    print(f"   未知的返回格式: {img_data.keys()}")
                
                if result_image:
                    result_path = OUTPUT_DIR / f"{timestamp}_4_result_json.png"
                    result_image.save(result_path)
                    print(f"   保存结果: {result_path}")
                    
                    # 分析结果
                    print("\n📊 分析结果...")
                    try:
                        import numpy as np
                        analysis = analyze_result(original_image, mask, result_image)
                        print(f"   平均像素差异（保留区域）: {analysis['mean_pixel_diff']:.2f}")
                        print(f"   最大像素差异: {analysis['max_pixel_diff']:.2f}")
                        print(f"   完全匹配比例: {analysis['exact_match_ratio']:.2%}")
                        print(f"   结论: {analysis['verdict']}")
                    except ImportError:
                        print("   ⚠️ 需要 numpy 来分析结果: pip install numpy")
        except Exception as e:
            print(f"   ⚠️ 解析返回数据时出错: {e}")
    else:
        print(f"❌ JSON 格式请求失败")
        print(f"   错误: {result1.get('error', 'Unknown error')}")
    
    # 6. 调用 API - 尝试 multipart 格式
    print("\n" + "=" * 40)
    print("📡 测试 2: Multipart 格式请求")
    print("=" * 40)
    
    result2 = call_painter_inpainting_multipart(
        url=painter_url,
        token=painter_token,
        image=original_image,
        mask=mask,
        prompt="a bright yellow sun in a clear blue sky"
    )
    
    if result2["success"]:
        print("✅ Multipart 格式请求成功!")
        # 同样的解析逻辑...
        try:
            data = result2["data"]
            if "data" in data and len(data["data"]) > 0:
                img_data = data["data"][0]
                if "b64_json" in img_data:
                    result_image = base64_to_image(img_data["b64_json"])
                elif "url" in img_data:
                    print(f"   返回了 URL: {img_data['url']}")
                    resp = requests.get(img_data["url"])
                    result_image = Image.open(BytesIO(resp.content))
                else:
                    result_image = None
                
                if result_image:
                    result_path = OUTPUT_DIR / f"{timestamp}_5_result_multipart.png"
                    result_image.save(result_path)
                    print(f"   保存结果: {result_path}")
        except Exception as e:
            print(f"   ⚠️ 解析返回数据时出错: {e}")
    else:
        print(f"❌ Multipart 格式请求失败")
        print(f"   错误: {result2.get('error', 'Unknown error')}")
    
    # 7. 总结
    print("\n" + "=" * 60)
    print("📋 总结")
    print("=" * 60)
    print(f"输出目录: {OUTPUT_DIR}")
    print(f"生成的文件:")
    for f in sorted(OUTPUT_DIR.glob(f"{timestamp}_*")):
        print(f"   - {f.name}")
    
    print("\n💡 下一步:")
    print("   1. 查看 poc_output/ 目录中的图片")
    print("   2. 对比原图和结果图，看 mask 外的区域是否一致")
    print("   3. 如果完全不一致，说明 API 可能不支持真正的 inpainting")


if __name__ == "__main__":
    main()
