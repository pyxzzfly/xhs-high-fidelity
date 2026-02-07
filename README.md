# XHS-High-Fidelity (小红书高保真重绘专家)

## 文档

- [业务需求文档 (BRD)](docs/BRD.md)
- [部署文档 (Linux + CUDA)](docs/DEPLOYMENT.md)
- [技术文档](docs/TECHNICAL.md)

## 快速启动（Linux 推荐）

```bash
chmod +x scripts/bootstrap_linux.sh scripts/stop_all.sh
bash scripts/bootstrap_linux.sh
```

## 核心理念
解决传统 Img2img 容易把电商产品细节（Logo/包装文字）改坏的问题：
- **主体保真优先**：通过抠图得到产品 alpha/mask，尽量把可编辑区域限制在背景
- **真实感优先**：以“背景局部改写（mask edit）+ 细节保真 + 比例约束”为主，降低贴图感与物理不合理

## 架构设计

### 1. 核心流程（简化）

- 原稿采集（XHS）：HTTP 尝试 -> Playwright 持久化 profile（按需弹窗登录）-> 自动重试 -> cookie 落盘
- 洗稿：Brain（OpenAI-compatible chat.completions）生成结构化 JSON 文案
- 配图仿写（B 图）：matting 得到产品 mask -> Painter `image+mask` 做背景局部改写 -> 细节保真/比例约束 -> 输出 runs
- 高保真换背景：参考图前景移除（近似）-> 放入新产品 -> 阴影/色彩协调 -> 输出 runs

### 2. 项目结构
- `backend/`: FastAPI 服务
- `frontend/`: Streamlit 交互界面
- `matting-service/`: 抠图 sidecar（建议开启，提升“保真/真实感”）
- `assets/`: 产物与临时文件（`assets/runs`）

## 快速验证 (PoC)
运行 `scripts/poc_inpainting.py`（实验性）测试 Painter API 能力。
