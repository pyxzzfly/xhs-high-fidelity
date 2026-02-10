# 部署（最简版：单机 Linux，一键启动，不用 Docker）

目标：在一台服务器上跑起来 `backend + frontend (+ matting 可选)`，并允许局域网访问前端页面。

端口（默认）：
- 后端 FastAPI：`8000`
- 前端 Streamlit：`8501`
- 抠图 sidecar（可选）：`8911`

说明：
- 前端已改为“**服务端拉图再渲染**”，所以局域网其他电脑访问前端时，只需要能访问 `8501`（不需要能直接访问 `8000`）。
- 小红书采集的“按需弹窗登录”需要 GUI（能打开 Chromium 窗口）。纯命令行服务器没有 GUI 时，只能依赖 cookie/relay 兜底。

## 0) 系统准备（只做一次）

Ubuntu（或等价发行版）：

```bash
sudo apt update
sudo apt install -y git curl python3 python3-venv
```

如果你要用 CUDA（可选检查）：

```bash
nvidia-smi
```

## 1) 一键部署 + 一键启动（推荐）

在服务器上从零开始（前提：网络可连通，能 `git clone` + `pip install`）：

```bash
git clone https://github.com/pyxzzfly/xhs-high-fidelity.git xhs-high-fidelity
cd xhs-high-fidelity

# 第一次启动建议先跑后端 .env 配置，否则洗稿/画图可能不可用
cp backend/.env.example backend/.env
vim backend/.env

# 一键安装依赖 + 启动（后台运行，日志写到 logs/）
chmod +x scripts/bootstrap_linux.sh scripts/stop_all.sh
bash scripts/bootstrap_linux.sh
```

访问：
- 同机：`http://127.0.0.1:8501`
- 局域网：`http://<server-ip>:8501`

停止：

```bash
bash scripts/stop_all.sh
```

日志：
- `logs/backend-uvicorn.log`
- `logs/frontend-streamlit.log`
- （可选）`logs/matting-uvicorn.log`

可选：同时启动 matting（抠图）sidecar：

```bash
ENABLE_MATTING=1 bash scripts/bootstrap_linux.sh
```

## 2) 必配项（backend/.env）

后端启动会自动读取 `backend/.env`（从 `.env.example` 复制）。

最常用的几项：

- 洗稿（不配会报错）：
  - `BRAIN_API_KEY`
  - `BRAIN_BASE_URL`
  - `BRAIN_MODEL`
- 画图/重绘（不配会报错；用于 `/ab_images` 与 `/generate`）：
  - `PAINTER_EDIT_URL`
  - `PAINTER_TOKEN`
  - `PAINTER_MODEL=google/nano-banana`（网关里的 Banana Pro）
  - （可选）`ENFORCE_GOOGLE_MODELS=true`：开启后强制使用 Google 系列模型（防止误配）
- 小红书采集（强烈建议配，且首次会弹窗让你登录一次）：
  - `XHS_USER_DATA_DIR=/home/<user>/.xhs-playwright-profile`
  - `XHS_PLAYWRIGHT_HEADLESS=false`
- 配图仿写并发（可选，越大越快，但更吃 GPU/画图服务吞吐）：
  - `AB_IMAGES_CONCURRENCY=2`（默认 2，可按机器调到 4/6）

（可选）配图仿写质量 V2（默认无需改；效果不稳再调）：
- `AB_IMAGES_ENGINE=v2_mask`（默认）| `v1_fullimg_pasteback`（旧逻辑兜底）
- `AB_PRODUCT_CORE_THRESHOLD=224`：产品核心阈值（越大越偏向“只保护产品本体”）
- `AB_PRODUCT_MASK_OPEN_PX=3`：核心 mask opening 像素（用于去掉阴影突起等噪声）
- `AB_PRODUCT_CORE_ERODE_PX=0`：可选进一步收缩核心保护区（过大可能误伤文字/Logo）
- `AB_PRODUCT_PROTECT_THRESHOLD=200`：保护区阈值（越小保护越大，越不容易动到产品边缘）
- `AB_MASK_PROTECT_DILATE_PX=4`：保护区膨胀像素（越大越不容易改到主体边缘，但更容易残留原图阴影/倒影）
- `AB_V2_AGGRESSIVE_EDIT_ERODE_PX=2`：aggressive 档额外收紧可编辑区域（进一步避免侵入产品边缘）
- `AB_MAX_BBOX_RATIO_DELTA=0.08`：激进档“主体占比漂移阈值”
- `AB_DETAIL_TRANSFER=1`：细节保真（高频细节迁移）开关
- `AB_DETAIL_TRANSFER_ALPHA=0.22`：细节迁移强度
- `AB_DETAIL_TRANSFER_BLUR_RADIUS=2.0`：细节高通 blur 半径
- `AB_DETAIL_TRANSFER_THRESHOLD=224`：细节迁移阈值（默认同产品核心阈值）
- `AB_DETAIL_TRANSFER_INNER_ERODE_PX=8`：细节迁移内缩像素（避免把边缘阴影一起迁回）

> 提醒：V2 为了降低贴图感/比例失控，默认依赖 matting sidecar 提供产品 mask。建议用 `ENABLE_MATTING=1` 启动（脚本会自动启动 sidecar）。

（可选）日志：
- `LOG_LEVEL=INFO`
- `XHS_LOG_STAGE=1`

## 3) 手动启动（不想用脚本时）

后端：

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -U pip
pip install -r requirements.txt
python -m playwright install --with-deps chromium
cp .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

前端：

```bash
cd frontend
python3 -m venv venv
source venv/bin/activate
pip install -U pip
pip install -r requirements.txt
streamlit run app.py --server.address 0.0.0.0 --server.port 8501
```

## 4) 常见问题（只保留最常见的）

### 4.1 采集返回 401（登录/协议页）
- 确保 `XHS_PLAYWRIGHT_HEADLESS=false`
- 确保机器有 GUI（能弹出浏览器窗口）
- 触发一次采集，在弹窗里登录一次，后续复用 `XHS_USER_DATA_DIR`
- 采集成功后浏览器会自动关闭；如果仍是 401，会保持窗口不关方便你继续登录

### 4.2 采集返回 429（300012/IP 风控）
- 登录无用，换网络/代理/降频。

### 4.3 采集返回 404（你访问的页面不存在）
- 可能是真删/不可见，也可能是风控伪装。
- 若你在真实 Chrome 能看正文但采集不行，用 `/api/v1/xhs/extract_relay` 兜底。

### 4.4 抠图 matting 报 502 Bad Gateway

这通常表示后端调用 `http://127.0.0.1:8911/matting` 时没有直连到本机服务（被系统代理/网关劫持），或 sidecar 没启动。

排查：
- 确认 sidecar 存活：`curl http://127.0.0.1:8911/health`
- 如果你机器配置了 `HTTP_PROXY/HTTPS_PROXY`，确保 `NO_PROXY` 包含 `127.0.0.1,localhost`（或直接临时 unset 代理变量）
- 确保你运行的是最新版后端（后端会禁用环境代理来访问 matting，避免 localhost 被代理转发）

## 5) 为什么你压缩很大（目录很大）

常见原因是你把本地的 `venv/`、`assets/runs/`、`__pycache__/`、日志等一起打包了。

建议：
- **优先用 `git clone` 在服务器上拉代码**，不要把本地 venv 打包过去。
- 如果必须打包：压缩前删除 `backend/venv/ frontend/venv/ matting-service/venv/ assets/runs/ logs/`。

## 6) 安全
- `backend/.env` 可能包含 `BRAIN_API_KEY`、`XHS_COOKIE`，不要提交到 git，不要随意分享。
