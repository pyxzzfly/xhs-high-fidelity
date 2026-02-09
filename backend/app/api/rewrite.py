import logging

from fastapi import APIRouter, Form, HTTPException
from pathlib import Path

from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError

from app.services.vision import VisionService
from app.services.rewrite_service import RewriteService

router = APIRouter()
logger = logging.getLogger("xhs-high-fidelity")

vision_service = VisionService()
rewrite_service = RewriteService(
    vision=vision_service,
    prompt_path=str(Path(__file__).resolve().parents[1] / "prompts" / "rewrite_article.txt"),
)


@router.post("/rewrite")
async def rewrite_article(
    template_id: str = Form("LIST_REVIEW"),
    product_name: str = Form(""),
    product_features: str = Form(""),
    original_text: str = Form(...),
):
    try:
        return rewrite_service.rewrite_one(
            template_id=template_id,
            product_name=product_name,
            product_features=product_features,
            original_text=original_text,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RateLimitError as exc:
        logger.warning("rewrite rate limited: %s", exc)
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except (APIConnectionError, APITimeoutError) as exc:
        logger.error("rewrite upstream unavailable: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except APIStatusError as exc:
        # Preserve upstream semantics (e.g. 502/503) instead of reporting as 400.
        status = int(getattr(exc, "status_code", None) or 502)
        logger.error("rewrite upstream error status=%s: %s", status, exc)
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("rewrite failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
