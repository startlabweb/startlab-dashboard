from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import database as db
from app.worker.manager import worker_manager

router = APIRouter()


class CreateMonitorRequest(BaseModel):
    sheet_url: str
    sheet_id: str
    sheet_name: str = "Form Responses 1"
    sheet_title: str | None = None
    video_column: str | None = None
    written_score_column: str = "Puntaje Preguntas"
    written_explanation_column: str = "Explicación"
    video_score_column: str = "Puntaje Roleplay"
    video_explanation_column: str = "Explicación"


@router.get("")
async def list_monitors():
    monitors = db.list_monitors()
    # Add candidate counts
    for m in monitors:
        m["counts"] = db.count_candidates(m["id"])
    return monitors


@router.post("")
async def create_monitor(req: CreateMonitorRequest):
    data = {
        "sheet_id": req.sheet_id,
        "sheet_url": req.sheet_url,
        "sheet_name": req.sheet_name,
        "sheet_title": req.sheet_title,
        "video_column": req.video_column,
        "written_score_column": req.written_score_column,
        "written_explanation_column": req.written_explanation_column,
        "video_score_column": req.video_score_column,
        "video_explanation_column": req.video_explanation_column,
        "status": "paused",
    }
    monitor = db.create_monitor(data)
    return monitor


@router.get("/{monitor_id}")
async def get_monitor(monitor_id: str):
    monitor = db.get_monitor(monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")
    monitor["counts"] = db.count_candidates(monitor_id)
    monitor["written_criteria"] = db.get_criteria_for_monitor(monitor_id, "written")
    monitor["video_criteria"] = db.get_criteria_for_monitor(monitor_id, "video")
    return monitor


@router.post("/{monitor_id}/start")
async def start_monitor(monitor_id: str):
    monitor = db.get_monitor(monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")

    # Check criteria are confirmed
    written_criteria = db.get_criteria_for_monitor(monitor_id, "written")
    video_criteria = db.get_criteria_for_monitor(monitor_id, "video")

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
    db.delete_monitor(monitor_id)
    return {"deleted": True}
