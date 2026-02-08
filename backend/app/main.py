from contextlib import asynccontextmanager
from pathlib import Path
import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

# torch is optional for "crawler-only" and "painter-only" workflows.
# Some environments (e.g. macOS + newer Python) may not have a compatible wheel.
try:  # pragma: no cover
    import torch  # type: ignore
except Exception as exc:  # pragma: no cover
    torch = None  # type: ignore[assignment]
    _TORCH_IMPORT_ERROR = exc
else:  # pragma: no cover
    _TORCH_IMPORT_ERROR = None

# Load backend/.env as early as possible so module-level singletons that read env
# (e.g. VisionService, PainterClient) see the correct config.
_backend_dir = Path(__file__).resolve().parents[1]
load_dotenv(dotenv_path=_backend_dir / ".env", override=False)

from app.core.gpu import gpu_lock
from app.core.logger import setup_logger

from app.api import ab_images
from app.api import flow
from app.api import generate
from app.api import rewrite
from app.api import xhs

logger = setup_logger()

@asynccontextmanager
async def lifespan(app: FastAPI):
    cookie_len = len((os.getenv("XHS_COOKIE") or "").strip())
    user_data_dir = (os.getenv("XHS_USER_DATA_DIR") or "").strip()

    logger.info("Starting XHS High Fidelity Backend...")
    logger.info(f"XHS env loaded: cookie_len={cookie_len}, user_data_dir={'set' if user_data_dir else 'empty'}")
    # Pre-flight check for GPU
    if torch is None:
        logger.warning(f"torch not available (GPU checks disabled): {_TORCH_IMPORT_ERROR}")
    else:
        if torch.cuda.is_available():
            logger.info(f"GPU Detected: {torch.cuda.get_device_name(0)}")
        elif torch.backends.mps.is_available():
            logger.info("MPS (Mac) Detected")
        else:
            logger.warning("No GPU detected, running on CPU (Slow!)")
    
    yield
    
    logger.info("Shutting down...")

app = FastAPI(
    title="XHS High Fidelity Tool",
    version="1.0.0",
    lifespan=lifespan
)

app.include_router(generate.router, prefix="/api/v1")
app.include_router(flow.router, prefix="/api/v1")
app.include_router(rewrite.router, prefix="/api/v1")
app.include_router(xhs.router, prefix="/api/v1")
app.include_router(ab_images.router, prefix="/api/v1")

# Serve run artifacts (final.png, masks, etc.)
# backend/app/main.py -> backend/app -> backend -> repo_root
_repo_root = Path(__file__).resolve().parents[2]
# Prefer repo_root/assets/runs as the default (matches api/generate.py and api/ab_images.py).
# Keep XHS_HF_OUTPUT_DIR as an override for custom deployments.
_assets_root = Path(os.getenv("XHS_HF_OUTPUT_DIR", str(_repo_root / "assets" / "runs")))
_assets_root.mkdir(parents=True, exist_ok=True)
app.mount("/runs", StaticFiles(directory=str(_assets_root)), name="runs")

@app.get("/health")
async def health_check():
    return {"status": "ok"}

@app.get("/gpu-health")
async def gpu_health():
    async with gpu_lock():
        try:
            # Simple tensor operation to check health
            if torch is None:
                return {"status": "ok", "device": "cpu", "torch": False, "error": str(_TORCH_IMPORT_ERROR)}
            if torch.cuda.is_available():
                x = torch.ones(1).cuda()
                y = x + 1
                return {"status": "ok", "device": "cuda", "vram_free": "TODO"}
            elif torch.backends.mps.is_available():
                x = torch.ones(1).to("mps")
                y = x + 1
                return {"status": "ok", "device": "mps"}
            else:
                return {"status": "ok", "device": "cpu", "torch": True}
        except Exception as e:
            logger.error(f"GPU Health Check Failed: {str(e)}")
            raise HTTPException(status_code=500, detail="GPU Failure")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
