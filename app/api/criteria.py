import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import database as db
from app.services.criteria_parser import parse_criteria, generate_evaluation_prompt

router = APIRouter()


class CriteriaUploadRequest(BaseModel):
    raw_text: str
    criteria_type: str  # 'written' or 'video'


class CriteriaConfirmRequest(BaseModel):
    parsed_criteria: list[dict] | None = None  # optional edits


@router.post("/{monitor_id}/criteria")
async def upload_criteria(monitor_id: str, req: CriteriaUploadRequest):
    monitor = db.get_monitor(monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")

    if req.criteria_type not in ("written", "video"):
        raise HTTPException(status_code=400, detail="criteria_type must be 'written' or 'video'")

    # Parse with AI
    parsed = await asyncio.to_thread(parse_criteria, req.raw_text, req.criteria_type)

    if "error" in parsed:
        raise HTTPException(status_code=400, detail=parsed["error"])

    # Generate GPT prompt template
    prompt_template = generate_evaluation_prompt(parsed["criteria"], parsed["total_points"], req.criteria_type)

    # Save to DB
    criteria_data = {
        "monitor_id": monitor_id,
        "criteria_type": req.criteria_type,
        "raw_text": req.raw_text,
        "parsed_criteria": parsed["criteria"],
        "total_points": parsed["total_points"],
        "gpt_prompt_template": prompt_template,
        "confirmed": False,
    }

    # Check if criteria already exists for this type
    existing = db.get_criteria_for_monitor(monitor_id, req.criteria_type)
    if existing:
        criteria = db.update_criteria(existing["id"], criteria_data)
    else:
        criteria = db.create_criteria(criteria_data)

    return {
        "id": criteria.get("id"),
        "criteria_type": req.criteria_type,
        "parsed_criteria": parsed["criteria"],
        "total_points": parsed["total_points"],
        "notes": parsed.get("notes", ""),
        "confirmed": False,
    }


@router.get("/{monitor_id}/criteria/{criteria_type}")
async def get_criteria(monitor_id: str, criteria_type: str):
    criteria = db.get_criteria_for_monitor(monitor_id, criteria_type)
    if not criteria:
        raise HTTPException(status_code=404, detail="Criteria not found")
    return criteria


@router.post("/{monitor_id}/criteria/{criteria_type}/confirm")
async def confirm_criteria(monitor_id: str, criteria_type: str, req: CriteriaConfirmRequest):
    criteria = db.get_criteria_for_monitor(monitor_id, criteria_type)
    if not criteria:
        raise HTTPException(status_code=404, detail="Criteria not found")

    update_data = {"confirmed": True}

    # If user edited the criteria, regenerate the prompt
    if req.parsed_criteria:
        total = sum(c.get("max_points", 0) for c in req.parsed_criteria)
        prompt_template = generate_evaluation_prompt(req.parsed_criteria, total, criteria_type)
        update_data.update({
            "parsed_criteria": req.parsed_criteria,
            "total_points": total,
            "gpt_prompt_template": prompt_template,
        })

    updated = db.update_criteria(criteria["id"], update_data)
    db.log_activity(monitor_id, "criteria_confirmed", f"Criteria ({criteria_type}) confirmed: {criteria.get('total_points')} pts")
    return updated
