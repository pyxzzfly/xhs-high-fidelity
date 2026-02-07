from __future__ import annotations

import uuid

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import Response

from app.core.logger import TaskLogger
from app.services.xhs_crawler import XHSCrawlError, crawl_xhs_note_light_async

router = APIRouter(prefix="/xhs", tags=["xhs"])


@router.post("/extract")
async def extract_xhs(
    request: Request,
    source_text: str = Form(..., description="小红书分享文案或链接"),
):
    """Best-effort extraction via HTTP + (optional) Playwright.

    For high reliability under XHS risk-control, prefer /xhs/extract_relay.
    """
    source_text = (source_text or "").strip()
    if not source_text:
        raise HTTPException(status_code=400, detail="source_text required")

    trace_id = (request.headers.get("X-Trace-Id") or "").strip() or str(uuid.uuid4())
    task = TaskLogger(trace_id)

    try:
        task.info("xhs.stage", stage="extract_start")
        return await crawl_xhs_note_light_async(source_text, trace_id=trace_id)
    except XHSCrawlError as exc:
        task.error("xhs.stage", stage="error", status_code=exc.status_code, error=str(exc))
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception as exc:
        task.error("xhs.stage", stage="error", status_code=500, error=str(exc))
        raise HTTPException(status_code=500, detail=f"xhs extract failed: {exc}") from exc


@router.post("/extract_relay")
async def extract_xhs_relay(
    source_text: str = Form(..., description="小红书分享文案或链接"),
):
    """High-reliability extraction via attaching to user's real Chrome (CDP).

    Requirements:
    - OpenClaw Gateway running (provides local CDP endpoint).
    - Chrome tab opened to the note (or we will open a new tab).

    This avoids Playwright 'new browser' signatures that often trigger XHS risk-control.
    """
    source_text = (source_text or "").strip()
    if not source_text:
        raise HTTPException(status_code=400, detail="source_text required")

    try:
        from app.services.xhs_crawler import crawl_xhs_note_from_cdp_async

        return await crawl_xhs_note_from_cdp_async(source_text)
    except XHSCrawlError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"xhs extract_relay failed: {exc}") from exc


@router.get("/image")
def proxy_image(url: str = Query(..., description="XHS CDN image url")):
    try:
        from app.services.xhs_image_proxy import fetch_xhs_image

        data, ctype = fetch_xhs_image(url)
        return Response(content=data, media_type=ctype)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"image proxy failed: {exc}") from exc
