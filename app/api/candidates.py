from fastapi import APIRouter, HTTPException

from app import database as db

router = APIRouter()


@router.get("/{monitor_id}/candidates")
async def list_candidates(monitor_id: str, limit: int = 100, offset: int = 0):
    candidates = db.list_candidates(monitor_id, limit=limit, offset=offset)
    counts = db.count_candidates(monitor_id)
    return {"candidates": candidates, "counts": counts}


@router.get("/{monitor_id}/candidates/{candidate_id}")
async def get_candidate(monitor_id: str, candidate_id: str):
    candidate = db.get_candidate(candidate_id)
    if not candidate or candidate.get("monitor_id") != monitor_id:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return candidate


@router.post("/{monitor_id}/candidates/{candidate_id}/retry")
async def retry_candidate(monitor_id: str, candidate_id: str):
    candidate = db.get_candidate(candidate_id)
    if not candidate or candidate.get("monitor_id") != monitor_id:
        raise HTTPException(status_code=404, detail="Candidate not found")

    update = {
        "error_message": None,
    }
    if candidate.get("written_status") == "error":
        update["written_status"] = "pending"
    if candidate.get("video_status") == "error":
        update["video_status"] = "pending"

    updated = db.update_candidate(candidate_id, update)
    return updated
