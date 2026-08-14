import asyncio

from fastapi import APIRouter, HTTPException

from app import database as db
from app.config import settings

router = APIRouter()

# La tabla del dashboard usa 7 campos. Traer select("*") con 300 filas arrastra
# `transcript` y `written_answers` completos: varios MB por request, cada 30s.
# El transcript se trae solo al abrir el detalle de un candidato.
CAMPOS_TABLA = (
    "id,sheet_row,name,email,video_source,video_url,"
    "written_status,written_score,written_explanation,"
    "video_status,video_score,video_explanation,"
    "attempts,error_message,cost_usd,processed_at,created_at,"
    "sheet_synced_at,lease_expires_at"
)


@router.get("/{monitor_id}/candidates")
async def list_candidates(
    monitor_id: str,
    limit: int = 500,
    offset: int = 0,
    status: str | None = None,
):
    """Lista candidatos.

    El default era 100 sin paginacion en la UI: con 300 candidatos no se veian
    200. `status` filtra por estado — `?status=error` es lo que el equipo va a
    mirar cuando revise los que quedaron afuera.
    """
    limit = max(1, min(limit, 1000))
    candidates = await asyncio.to_thread(
        db.list_candidates,
        monitor_id,
        limit,
        offset,
        CAMPOS_TABLA,
        status,
    )
    counts = await asyncio.to_thread(db.count_candidates, monitor_id)
    return {"candidates": candidates, "counts": counts}


@router.get("/{monitor_id}/progress")
async def progress(monitor_id: str):
    """Instrumento de vuelo para las horas de la rafaga.

    `sheet_dirty` es el numero que importa: "todo evaluado pero el Sheet vacio"
    es el escenario que arruina la entrega, y hoy es invisible.
    """
    datos = await asyncio.to_thread(db.monitor_progress, monitor_id)
    if datos is None:
        raise HTTPException(status_code=404, detail="Monitor not found")
    return datos


@router.get("/{monitor_id}/candidates/{candidate_id}")
async def get_candidate(monitor_id: str, candidate_id: str):
    candidate = await asyncio.to_thread(db.get_candidate, candidate_id)
    if not candidate or candidate.get("monitor_id") != monitor_id:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return candidate


@router.post("/{monitor_id}/candidates/{candidate_id}/retry")
async def retry_candidate(monitor_id: str, candidate_id: str):
    """Reencola un candidato.

    Antes solo actuaba sobre status 'error', asi que un candidato colgado en
    'processing' no se podia recuperar desde la UI. Ahora tambien resetea
    `attempts` y el lease, que es lo que lo hace visible para la cola.
    """
    candidate = await asyncio.to_thread(db.get_candidate, candidate_id)
    if not candidate or candidate.get("monitor_id") != monitor_id:
        raise HTTPException(status_code=404, detail="Candidate not found")

    update = {
        "error_message": None,
        "attempts": 0,
        "worker_id": None,
        "lease_expires_at": db.EPOCH,
    }
    if candidate.get("written_status") in ("error", "processing"):
        update["written_status"] = "pending"
    if candidate.get("video_status") in ("error", "processing"):
        update["video_status"] = "pending"

    updated = await asyncio.to_thread(db.update_candidate, candidate_id, update)
    return updated


@router.post("/{monitor_id}/retry-all")
async def retry_all(monitor_id: str):
    """Reencola todo lo que no termino. Es el boton que se aprieta si algo se trabo."""
    n = await asyncio.to_thread(db.retry_all_unfinished, monitor_id)
    await asyncio.to_thread(
        db.log_activity, monitor_id, "retry_all", f"{n} candidatos reencolados a mano"
    )
    return {"requeued": n}


@router.post("/{monitor_id}/sync-sheet")
async def sync_sheet(monitor_id: str, force: bool = False):
    """Vuelca los resultados de la base al Google Sheet.

    Con force=true reescribe TODO ignorando `sheet_synced_at`: reconstruye las 300
    celdas en ~2 requests. Es el plan B nivel 0.
    """
    from tools.sheet_sync import sync_completed_to_sheet

    monitor = await asyncio.to_thread(db.get_monitor, monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")

    resultado = await asyncio.to_thread(sync_completed_to_sheet, monitor, force, None)
    await asyncio.to_thread(
        db.log_activity, monitor_id, "sheet_sync", f"Sync manual (force={force}): {resultado}"
    )
    return resultado


@router.get("/{monitor_id}/queue")
async def queue_state(monitor_id: str):
    """Estado crudo de la cola, para diagnosticar sin la UI."""
    from tools import sheets_limiter

    datos = await asyncio.to_thread(db.queue_state, monitor_id, settings.MAX_ATTEMPTS)
    datos["sheets_requests_last_min"] = sheets_limiter.uso_actual()
    datos["worker_id"] = db.WORKER_ID
    datos["concurrency"] = settings.WORKER_CONCURRENCY
    return datos
