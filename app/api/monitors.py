import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import database as db
from app.worker.manager import worker_manager

router = APIRouter()


EVALUATOR_DEFAULTS = {
    "sales": {
        "written_score_column": "Puntaje Preguntas",
        "written_explanation_column": "Explicación",
        "video_score_column": "Puntaje Roleplay",
        "video_explanation_column": "Explicación",
    },
    "editor": {
        "written_score_column": "Puntaje Total",
        "written_explanation_column": "Explicación",
        "video_score_column": None,
        "video_explanation_column": None,
    },
}


class CreateMonitorRequest(BaseModel):
    sheet_url: str
    sheet_id: str
    sheet_name: str = "Form Responses 1"
    sheet_title: str | None = None
    evaluator_type: Literal["sales", "editor"] = "sales"
    video_column: str | None = None
    written_score_column: str | None = None
    written_explanation_column: str | None = None
    video_score_column: str | None = None
    video_explanation_column: str | None = None


@router.get("")
async def list_monitors():
    # Las llamadas a Supabase son sincronas: van a un thread para no bloquear
    # el event loop (si no, cada request encola a todas las demas).
    monitors = await asyncio.to_thread(db.list_monitors)
    counts = await asyncio.to_thread(
        db.count_candidates_bulk, [m["id"] for m in monitors]
    )
    for m in monitors:
        m["counts"] = counts.get(m["id"], {})
    return monitors


@router.post("")
async def create_monitor(req: CreateMonitorRequest):
    defaults = EVALUATOR_DEFAULTS[req.evaluator_type]

    data = {
        "sheet_id": req.sheet_id,
        "sheet_url": req.sheet_url,
        "sheet_name": req.sheet_name,
        "sheet_title": req.sheet_title,
        "evaluator_type": req.evaluator_type,
        "video_column": req.video_column if req.evaluator_type == "sales" else None,
        "written_score_column": req.written_score_column or defaults["written_score_column"],
        "written_explanation_column": req.written_explanation_column or defaults["written_explanation_column"],
        "video_score_column": (req.video_score_column or defaults["video_score_column"]) if req.evaluator_type == "sales" else None,
        "video_explanation_column": (req.video_explanation_column or defaults["video_explanation_column"]) if req.evaluator_type == "sales" else None,
        "status": "paused",
    }
    monitor = await asyncio.to_thread(db.create_monitor, data)
    return monitor


@router.get("/{monitor_id}")
async def get_monitor(monitor_id: str):
    monitor = await asyncio.to_thread(db.get_monitor, monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")

    # Los tres son independientes: van en paralelo en vez de uno tras otro.
    counts, written, video = await asyncio.gather(
        asyncio.to_thread(db.count_candidates, monitor_id),
        asyncio.to_thread(db.get_criteria_for_monitor, monitor_id, "written"),
        asyncio.to_thread(db.get_criteria_for_monitor, monitor_id, "video"),
    )
    monitor["counts"] = counts
    monitor["written_criteria"] = written
    monitor["video_criteria"] = video
    return monitor


@router.post("/{monitor_id}/start")
async def start_monitor(monitor_id: str):
    monitor = await asyncio.to_thread(db.get_monitor, monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")

    # Check criteria are confirmed
    written_criteria = await asyncio.to_thread(
        db.get_criteria_for_monitor, monitor_id, "written"
    )

    if not written_criteria or not written_criteria.get("confirmed"):
        raise HTTPException(status_code=400, detail="Written criteria not confirmed")

    await worker_manager.start_monitor(monitor_id)
    return {"status": "active"}


@router.post("/{monitor_id}/stop")
async def stop_monitor(monitor_id: str):
    await worker_manager.stop_monitor(monitor_id)
    return {"status": "paused"}


@router.delete("/{monitor_id}")
async def delete_monitor(monitor_id: str):
    await worker_manager.stop_monitor(monitor_id)
    await asyncio.to_thread(db.delete_monitor, monitor_id)
    return {"deleted": True}
