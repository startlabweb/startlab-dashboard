from supabase import create_client, Client
from app.config import settings

_client: Client | None = None


def get_db() -> Client:
    global _client
    if _client is None:
        _client = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)
    return _client


# --- Monitors ---

def create_monitor(data: dict) -> dict:
    db = get_db()
    result = db.table("monitors").insert(data).execute()
    return result.data[0]


def get_monitor(monitor_id: str) -> dict | None:
    db = get_db()
    result = db.table("monitors").select("*").eq("id", monitor_id).execute()
    return result.data[0] if result.data else None


def list_monitors() -> list[dict]:
    db = get_db()
    result = db.table("monitors").select("*").order("created_at", desc=True).execute()
    return result.data


def update_monitor(monitor_id: str, data: dict) -> dict:
    db = get_db()
    result = db.table("monitors").update(data).eq("id", monitor_id).execute()
    return result.data[0] if result.data else {}


def delete_monitor(monitor_id: str):
    db = get_db()
    db.table("monitors").delete().eq("id", monitor_id).execute()


# --- Criteria ---

def create_criteria(data: dict) -> dict:
    db = get_db()
    result = db.table("criteria").insert(data).execute()
    return result.data[0]


def get_criteria_for_monitor(monitor_id: str, criteria_type: str) -> dict | None:
    db = get_db()
    result = (
        db.table("criteria")
        .select("*")
        .eq("monitor_id", monitor_id)
        .eq("criteria_type", criteria_type)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def update_criteria(criteria_id: str, data: dict) -> dict:
    db = get_db()
    result = db.table("criteria").update(data).eq("id", criteria_id).execute()
    return result.data[0] if result.data else {}


# --- Candidates ---

def create_candidate(data: dict) -> dict:
    db = get_db()
    result = db.table("candidates").insert(data).execute()
    return result.data[0]


def get_candidate(candidate_id: str) -> dict | None:
    db = get_db()
    result = db.table("candidates").select("*").eq("id", candidate_id).execute()
    return result.data[0] if result.data else None


def get_candidate_by_row(monitor_id: str, sheet_row: int) -> dict | None:
    db = get_db()
    result = (
        db.table("candidates")
        .select("*")
        .eq("monitor_id", monitor_id)
        .eq("sheet_row", sheet_row)
        .execute()
    )
    return result.data[0] if result.data else None


def list_candidates(monitor_id: str, limit: int = 100, offset: int = 0) -> list[dict]:
    db = get_db()
    result = (
        db.table("candidates")
        .select("*")
        .eq("monitor_id", monitor_id)
        .order("sheet_row", desc=False)
        .range(offset, offset + limit - 1)
        .execute()
    )
    return result.data


def update_candidate(candidate_id: str, data: dict) -> dict:
    db = get_db()
    result = db.table("candidates").update(data).eq("id", candidate_id).execute()
    return result.data[0] if result.data else {}


def count_candidates(monitor_id: str) -> dict:
    """Returns counts by status."""
    db = get_db()
    all_rows = (
        db.table("candidates")
        .select("written_status, video_status")
        .eq("monitor_id", monitor_id)
        .execute()
    )
    counts = {"total": 0, "completed": 0, "processing": 0, "pending": 0, "error": 0}
    for row in all_rows.data:
        counts["total"] += 1
        # Consider completed if written is done (video might be no_video)
        ws = row.get("written_status", "pending")
        vs = row.get("video_status", "pending")
        if ws == "completed" and vs in ("completed", "no_video"):
            counts["completed"] += 1
        elif ws == "error" or vs == "error":
            counts["error"] += 1
        elif ws == "processing" or vs == "processing":
            counts["processing"] += 1
        else:
            counts["pending"] += 1
    return counts


# --- Activity Log ---

def log_activity(monitor_id: str, event_type: str, message: str, metadata: dict | None = None):
    db = get_db()
    data = {
        "monitor_id": monitor_id,
        "event_type": event_type,
        "message": message,
    }
    if metadata:
        data["metadata"] = metadata
    db.table("activity_log").insert(data).execute()


def get_activity(monitor_id: str, limit: int = 50) -> list[dict]:
    db = get_db()
    result = (
        db.table("activity_log")
        .select("*")
        .eq("monitor_id", monitor_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data
