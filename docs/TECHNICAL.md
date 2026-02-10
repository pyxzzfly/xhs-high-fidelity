# 技术文档（维护/二次开发）

本文档面向维护者与二次开发者，目标是让你能快速理解：系统架构、关键链路、接口、配置、日志与排障。

## 1. 系统概览

### 1.1 组件图

```mermaid
graph TD
  U[用户浏览器] -->|HTTP| FE[Frontend: Streamlit :8501]
  FE -->|HTTP| BE[Backend: FastAPI :8000]
  BE -->|HTTP| XHS[小红书站点/CDN]
  BE -->|HTTP| BRAIN[Brain(OpenAI-compatible)
/ chat.completions]
  BE -->|HTTP| PAINTER[Painter/图像改写服务]
  BE -->|HTTP| MATTING[Matting Sidecar(rembg)
:8911]

  BE -->|静态文件| RUNS[/runs 产物目录]
```

### 1.2 代码结构

- `backend/app/main.py`：FastAPI app、路由注册、静态 `/runs` 挂载、dotenv 加载
- `backend/app/api/*`：HTTP API 层
- `backend/app/services/*`：核心业务逻辑
- `backend/app/core/logger.py`：JSON 日志 + `TaskLogger(trace_id)`
- `frontend/app.py`：Streamlit 交互界面（采集/洗稿/配图）
- `matting-service/`：抠图 sidecar（Python 3.11）

## 2. 关键业务链路

### 2.1 A：小红书采集 `/api/v1/xhs/extract`

目标：从小红书链接/分享文案中提取：标题、正文、原稿插图 URL 列表，并尽可能自动处理登录态。

核心机制（代码：`backend/app/services/xhs_crawler.py`）：
- URL/NoteId 解析（从分享文案抽取最像 note 的 URL）
- HTTP 轻量拉取（若命中 gate，则 fallback）
- Playwright 持久化 profile：`XHS_USER_DATA_DIR`
- Gate 检测：登录/协议页、风控 300012、伪装不存在 404
- 登录等待：若需要交互，则 headful 打开窗口，轮询等待用户登录/同意
- 自动重试：登录完成后 reload 目标页面，继续提取
- Cookie 落盘：成功提取后导出 cookie，写回 `backend/.env`（并更新 `os.environ`）

错误码约定（`XHSCrawlError(status_code=...)`）：
- `401`：登录/协议页拦截（需要用户在弹窗完成登录/同意）
- `409`：用户关闭浏览器/上下文失效
- `429`：风控 300012 / IP 风险（登录无用，建议换网络）
- `404`：页面不存在（真实删除/不可见，或风控伪装）

### 2.2 B：洗稿 `/api/v1/rewrite`

代码：`backend/app/api/rewrite.py` + `backend/app/services/rewrite_service.py`

- 输入：`original_text`（原稿全文），可选 `template_id/product_name/product_features`
- 调用：Brain(OpenAI-compatible) 的 `chat.completions.create`，要求输出 JSON
- 输出：`title/content/outline/hashtags/word_count/target_word_count/...`

依赖：
- `BRAIN_API_KEY`, `BRAIN_BASE_URL`, `BRAIN_MODEL`

错误码约定（便于排障）：
- `400`：入参问题（例如 `original_text` 为空）
- `429`：上游限流
- `502/503`：上游网关/模型服务不可用（例如 `BRAIN_BASE_URL` 返回 502）
- `500`：其他未分类异常

### 2.3 C：配图仿写 `/api/v1/ab_images`

代码：`backend/app/api/ab_images.py`

- 输入：原稿插图 URLs + 标题/要点 + 风格预设（ugc/glossy）
- 处理：
  - 通过 `xhs_image_proxy.fetch_xhs_image` 拉取原图
  - 默认走 **V2（背景局部改写 / mask edit）**：
    - Matting sidecar 提取产品 mask（用于“锁死主体、只改背景”）
    - 生成保护区/编辑 mask（核心思想：保护产品边缘，避免“产品被重绘后又叠加回去”的贴图感）：
      - `product_core`：高阈值 + opening +（可选）erosion，尽量只保留产品本体（用于细节保真/统计）
      - `product_protect`：比 core 更宽的保护区（用于“锁死产品边缘”），再做 dilate
      - `edit_mask = invert(product_protect_dilated)`
    - 调用 Painter `edit(image+mask)` 做背景改写（尽量保持主体形状/比例/文字不变）
    - UGC 相机质感后处理（`style_preset=ugc`）对**全图**生效：轻微噪点/压缩/暗角/手持角度，让产品与背景共享同一套“手机随手拍”质感，降低贴图感
      - V2 还会对“可编辑区域”（背景）额外做一点柔化，模拟景深
    - （可选）产品区域“高频细节迁移”，恢复包装文字/边缘纹理，降低贴图感
    - （仅激进）比例 gate：对输出重新估算产品 bbox 占比，漂移过大则自动降强度重试/回退
  - 可通过 `AB_IMAGES_ENGINE=v1_fullimg_pasteback` 切回旧逻辑（全图 img2img + 前景粘回）
  - 调用 Painter 生成 B 图（中等/激进）
  - **并行**：中等/激进会在后端并行生成（由 `AB_IMAGES_CONCURRENCY` 控制并发上限，默认 2）
  - **不叠加文字**（模板：`make_page_contain`）
  - 场景 token 做合理性约束（避免不合常理场景）
    - 会根据标题/要点做关键词识别，粗粒度推断产品类别（例如酒/饮品/零食/护肤/家清等）
    - 优先选择与产品用途更匹配的生活场景（例如酒更偏吃饭/聚会/下酒菜场景），让产品和场景更融洽
- 输出：`/runs/<task_id>/...png` 的相对 URL 列表 + `artifacts_dir`

### 2.4 D：高保真换背景 `/api/v1/generate`

代码：`backend/app/api/generate.py`

目标：保证产品主体像素级保真（Logo/文字不失真），尽量拟合参考图风格。

核心步骤（简化）：
- 确保 product RGBA（如无 alpha，调用 matting sidecar 抠图）
- 参考图分析（Vision/Heuristic）
- 参考替换：移除参考图原前景 -> 放入新产品 -> 生成阴影 -> harmonize
  - harmonize 为“边缘融合优先”（edge-only blend），以保住包装文字/Logo 不失真
  - 阴影方向会参考 `lighting_direction`（若 Vision 可用）
- 产物落盘：`assets/runs/<task_id>/final.png` 并通过 `/runs` 静态挂载提供

### 2.5 E：批处理 Flow `/api/v1/flow/*`

代码：`backend/app/api/flow.py` + `backend/app/domain/flow_state.py`

- `/flow/start`：一次传多个 reference_images，后台线程池并发跑 `generate_one`
- `/flow/status/{flow_id}`：查询进度与每项产物
- `/flow/retry/{flow_id}`：MVP 未实现（返回 501）
- `/flow/cancel/{flow_id}`：取消（内存状态）

注意：Flow 状态仅在内存 `_flow_store`，后端重启会丢。

## 3. API 一览（请求与响应要点）

以下为现有路由（`backend/app/api/*`）：

### 3.1 XHS

- `POST /api/v1/xhs/extract`（Form：`source_text`）
  - 返回：`title`, `content`, `reference_text`, `image_urls`, `image_count`, `canonical_url`, `note_id`
- `POST /api/v1/xhs/extract_relay`（Form：`source_text`）
  - 返回：同上（高可靠：依赖 Browser Relay）
- `GET /api/v1/xhs/image?url=...`
  - 返回：图片 bytes（使用 `XHS_COOKIE` 拉取受限资源）

### 3.2 Rewrite

- `POST /api/v1/rewrite`（Form：`original_text`, 可选 `template_id/product_name/product_features`）
  - 返回：JSON（标题、正文、要点、标签、字数等）

### 3.3 AB Images

- `POST /api/v1/ab_images`（Form：`image_urls_json`, `title`, `bullets_json`, `style_preset`, ...）
  - 返回：`b_medium_image_urls`, `b_aggressive_image_urls`, `artifacts_dir`, `b_error`（可选）

### 3.4 Generate

- `POST /api/v1/generate`（multipart：`product_image`, `reference_image` + Form 参数）
  - 返回：`task_id`, `image_url`, `artifacts_dir`, `analysis`, `scene_analysis`
- `POST /api/v1/generate_copy`（Form：`product_name`, `features`, `reference_text`）

### 3.5 Flow

- `POST /api/v1/flow/start`（multipart：`product_image`, `reference_images[]`）
- `GET /api/v1/flow/status/{flow_id}`
- `POST /api/v1/flow/retry/{flow_id}`（MVP 501）
- `DELETE /api/v1/flow/cancel/{flow_id}`

## 4. 配置与环境变量

来源：`backend/.env.example` + 代码默认值。

### 4.1 Brain（洗稿/多模态分析）

- `BRAIN_API_KEY`
- `BRAIN_BASE_URL`
- `BRAIN_MODEL`（默认 `gemini-3-pro`）

### 4.2 Painter（图像改写服务）

- `PAINTER_EDIT_URL`
- `PAINTER_TOKEN`
- `PAINTER_MODEL`（当前网关中 Banana Pro 对应 `google/nano-banana`）
- `ENFORCE_GOOGLE_MODELS`（可选，`true/false`）：
  - 开启后要求 `PAINTER_MODEL` 必须以 `google/` 开头
  - 同时要求 `BRAIN_MODEL` 必须包含 `gemini`

### 4.3 Matting Sidecar（抠图）

- `MATTING_BASE_URL`（默认 `http://127.0.0.1:8911`）
- `MATTING_URL`（legacy 名称，仍兼容）

说明：
- 后端调用 matting 时会禁用环境代理（httpx `trust_env=false`），避免 localhost 被代理转发导致 `502 Bad Gateway`。

### 4.4 AB Images 并发（可选）

- `AB_IMAGES_CONCURRENCY`（默认 `2`）：配图仿写时后端并发度。越大越快，但更吃 GPU/画图服务吞吐。

### 4.5 AB Images 质量（可选）

默认无需配置；当你遇到“贴图感/比例失控”时可调：

- `AB_IMAGES_ENGINE`：`v2_mask`（默认）| `v1_fullimg_pasteback`（旧逻辑兜底）
- `AB_PRODUCT_CORE_THRESHOLD`（默认 `224`）：核心阈值（越大越偏向“只保护产品本体”）
- `AB_PRODUCT_MASK_OPEN_PX`（默认 `3`）：核心 mask opening 像素（用于去掉阴影突起等噪声）
- `AB_PRODUCT_CORE_ERODE_PX`（默认 `0`）：可选进一步收缩核心保护区（过大可能误伤文字/Logo）
- `AB_PRODUCT_PROTECT_THRESHOLD`（默认 `AB_PRODUCT_CORE_THRESHOLD-40` 且最低 160）：
  - 保护区阈值（越小保护越大，越不容易改到产品边缘）
  - 用来减少“边缘被改写后又叠加回去”的贴图感
- `AB_MASK_PROTECT_DILATE_PX`（默认 `4`）：保护区膨胀像素（越大越不容易改到主体边缘，但也更容易残留原图阴影/倒影）
- `AB_V2_AGGRESSIVE_EDIT_ERODE_PX`（默认 `2`）：aggressive 档额外收紧可编辑区域，避免背景重绘侵入产品边缘
- `AB_MAX_BBOX_RATIO_DELTA`（默认 `0.08`）：激进档“主体占比漂移阈值”（超过会自动降强度重试/回退）
- `AB_DETAIL_TRANSFER`（默认 `1`）：是否启用“高频细节迁移”
- `AB_DETAIL_TRANSFER_ALPHA`（默认 `0.22`）：细节迁移强度（过大会变“硬贴”，过小会丢文字细节）
- `AB_DETAIL_TRANSFER_BLUR_RADIUS`（默认 `2.0`）：细节高通的 blur 半径
- `AB_DETAIL_TRANSFER_THRESHOLD`（默认同 `AB_PRODUCT_CORE_THRESHOLD`）：细节迁移阈值
- `AB_DETAIL_TRANSFER_INNER_ERODE_PX`（默认 `8`）：细节迁移内缩像素（避免把边缘阴影一起迁回）

### 4.6 XHS 采集（Playwright + Cookie）

- `XHS_USER_DATA_DIR`（必填）
- `XHS_PLAYWRIGHT_HEADLESS`（建议 false）
- `XHS_CRAWL_TIMEOUT`
- `XHS_AUTO_LOGIN_ON_401`
- `XHS_AUTO_LOGIN_WAIT_MS`
- `XHS_AUTO_LOGIN_POLL_INTERVAL_MS`
- `XHS_COOKIE_PERSIST_TO_ENV`
- `XHS_COOKIE_ENV_PATH`（可选）
- `XHS_COOKIE_PERSIST_MIN_INTERVAL_S`

### 4.7 日志

- `LOG_LEVEL`
- `XHS_LOG_STAGE`
- `XHS_LOG_DUP_TO_UVICORN`

## 5. 日志与 trace_id

`TaskLogger` 输出 JSON（`backend/app/core/logger.py`）：
- `timestamp/level/message/module/func/line`
- `trace_id`：一次请求或任务的链路 ID
- `props`：阶段字段（例如 `stage=extract_start/gate_detected/...`）

trace_id 规则：
- 若请求头携带 `X-Trace-Id`，优先使用
- 否则后端生成 `uuid4`

安全：
- 不记录 `XHS_COOKIE` 明文，只记录长度/sha 前缀。

## 6. 已知限制

- 小红书风控不可控：可能 401/429/伪装 404。
- Playwright “弹窗让人登录”需要 GUI 环境；纯服务器无 GUI 时，只能依赖 cookie/relay。
- Flow 状态只在内存，重启丢失。

## 7. 扩展路线（建议）

- 用 Celery/Redis 替换线程池，支持排队与持久化重试。
- Flow 状态落库（SQLite/Postgres）。
- 前端从 Streamlit 升级到 Web App（见 `docs/DESIGN_*` 作为路线图）。
