import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from tools.sheet_reader import extract_sheet_id, preview_sheet

router = APIRouter()


class SheetPreviewRequest(BaseModel):
    sheet_url: str


@router.post("/preview")
async def sheet_preview(req: SheetPreviewRequest):
    sheet_id = extract_sheet_id(req.sheet_url)
    if not sheet_id:
        raise HTTPException(status_code=400, detail="URL de Google Sheet invalida")

    try:
        data = await asyncio.to_thread(preview_sheet, sheet_id)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"No se pudo acceder al Sheet. Verifica que esté compartido con el service account. Error: {e}",
        )

    return {
        "sheet_id": sheet_id,
        "title": data["title"],
        "headers": data["headers"],
        "sample_rows": data["sample_rows"],
        "total_rows": data["total_rows"],
        "video_column": data["video_column"],
        "video_column_index": data["video_column_index"],
    }
