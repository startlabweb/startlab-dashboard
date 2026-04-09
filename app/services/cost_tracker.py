from app import database as db

# Approximate costs per candidate
COST_GEMINI_PER_CANDIDATE = 0.15
COST_GPT_VIDEO_PER_CANDIDATE = 0.02
COST_GPT_WRITTEN_PER_CANDIDATE = 0.02
COST_GPT_CRITERIA_PARSE = 0.03


def estimate_cost(n_candidates: int, n_with_video: int) -> dict:
    """Estimate processing cost for a batch."""
    written_cost = n_candidates * COST_GPT_WRITTEN_PER_CANDIDATE
    video_cost = n_with_video * (COST_GEMINI_PER_CANDIDATE + COST_GPT_VIDEO_PER_CANDIDATE)
    total = written_cost + video_cost

    return {
        "written_cost": round(written_cost, 2),
        "video_cost": round(video_cost, 2),
        "total": round(total, 2),
        "n_candidates": n_candidates,
        "n_with_video": n_with_video,
        "per_candidate_written": COST_GPT_WRITTEN_PER_CANDIDATE,
        "per_candidate_video": round(COST_GEMINI_PER_CANDIDATE + COST_GPT_VIDEO_PER_CANDIDATE, 2),
    }


def record_cost(monitor_id: str, candidate_id: str, cost_usd: float):
    """Record cost for a candidate and update monitor total."""
    db.update_candidate(candidate_id, {"cost_usd": cost_usd})
    monitor = db.get_monitor(monitor_id)
    current_total = float(monitor.get("total_cost_usd", 0))
    db.update_monitor(monitor_id, {"total_cost_usd": current_total + cost_usd})
