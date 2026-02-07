from fastapi import APIRouter, Form, HTTPException
from pathlib import Path

from app.services.vision import VisionService
from app.services.rewrite_service import RewriteService

router = APIRouter()

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
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
