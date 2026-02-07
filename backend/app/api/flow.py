from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from typing import List, Optional
import io
import os
from concurrent.futures import ThreadPoolExecutor
from PIL import Image

from app.domain.flow_state import create_flow, get_flow, update_item, recompute, cancel_flow
from app.api.generate import generate_one

router = APIRouter()

EXECUTOR = ThreadPoolExecutor(max_workers=int(os.getenv("FLOW_WORKERS", "4")))


@router.post("/flow/start")
async def flow_start(
    product_image: UploadFile = File(...),
    reference_images: List[UploadFile] = File(...),
    align_reference: bool = Form(True),
    exaggeration_level: str = Form("明显"),
    shadow: bool = Form(True),
):
    prod_bytes = await product_image.read()
    if not reference_images:
        raise HTTPException(status_code=400, detail="reference_images required")

    ref_bytes_list = []
    for f in reference_images:
        ref_bytes_list.append(await f.read())

    flow = create_flow(
        total=len(ref_bytes_list),
        exaggeration_level=exaggeration_level,
        align_reference=align_reference,
        shadow=shadow,
    )

    def _run_one(index: int, ref_bytes: bytes):
        update_item(flow.flow_id, index, status="processing", error=None)
        recompute(flow.flow_id)
        try:
            out = generate_one(
                prod_bytes=prod_bytes,
                ref_bytes=ref_bytes,
                align_reference=align_reference,
                exaggeration_level=exaggeration_level,
                shadow=shadow,
            )
            update_item(
                flow.flow_id,
                index,
                status="completed",
                artifacts_dir=out.get("artifacts_dir"),
                analysis=out.get("analysis"),
            )
        except Exception as exc:
            update_item(flow.flow_id, index, status="failed", error=str(exc)[:500])
        finally:
            recompute(flow.flow_id)

    for idx, ref_bytes in enumerate(ref_bytes_list):
        EXECUTOR.submit(_run_one, idx, ref_bytes)

    return {"flow_id": flow.flow_id, "status": flow.status, "total": flow.total}


@router.get("/flow/status/{flow_id}")
async def flow_status(flow_id: str):
    flow = get_flow(flow_id)
    if not flow:
        raise HTTPException(status_code=404, detail="flow not found")
    return {
        "flow_id": flow.flow_id,
        "status": flow.status,
        "total": flow.total,
        "completed": flow.completed,
        "failed": flow.failed,
        "progress": flow.progress,
        "items": [
            {
                "index": it.index,
                "status": it.status,
                "error": it.error,
                "artifacts_dir": it.artifacts_dir,
                "analysis": it.analysis,
            }
            for it in flow.items
        ],
    }


@router.post("/flow/retry/{flow_id}")
async def flow_retry(flow_id: str, index: int = Form(...)):
    flow = get_flow(flow_id)
    if not flow:
        raise HTTPException(status_code=404, detail="flow not found")
    if flow.status == "cancelled":
        raise HTTPException(status_code=400, detail="flow cancelled")
    if index < 0 or index >= flow.total:
        raise HTTPException(status_code=400, detail="index out of range")

    # NOTE: For MVP we don't store original bytes per flow. User should re-start flow.
    raise HTTPException(status_code=501, detail="retry not implemented in MVP; please restart flow")


@router.delete("/flow/cancel/{flow_id}")
async def flow_cancel(flow_id: str):
    ok = cancel_flow(flow_id)
    if not ok:
        raise HTTPException(status_code=404, detail="flow not found")
    return {"cancelled": True}
