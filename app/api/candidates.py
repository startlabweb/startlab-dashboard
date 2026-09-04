import asyncio

from fastapi import APIRouter, HTTPException

from app import database as db
from app.config import settings
from app.services import gates
from tools.motivos import razon_sin_video, resumen_error

router = APIRouter()


def _anotar_razones(candidate: dict, monitor: dict | None = None) -> dict:
    """Agrega los motivos legibles que la UI muestra junto al N/A y al error.

    Se calculan al leer (no se persisten): asi cubren tambien a los candidatos
    ingeridos antes de este cambio, porque salen de campos que ya se guardaban
    (`video_url` crudo y `error_message`).

    `estado_embudo` es la MISMA frase que se escribe en la planilla, calculada
    por `gates.estado_texto`. Se resuelve en el servidor a proposito: si el
    dashboard la reimplementara en JavaScript, la tabla y la planilla podrian
    decir cosas distintas sobre el mismo candidato.
    """
    if candidate.get("video_status") == "no_video":
        candidate["razon_na"] = razon_sin_video(candidate.get("video_url"))
    if candidate.get("error_message"):
        candidate["razon_error"] = resumen_error(candidate.get("error_message"))
    if monitor and (gates.gate1_configurado(monitor) or gates.etapa_iq_activa(monitor)):
        candidate["estado_embudo"] = gates.estado_texto(monitor, candidate)
    return candidate

# La tabla del dashboard usa 7 campos. Traer select("*") con 300 filas arrastra
# `transcript` y `written_answers` completos: varios MB por request, cada 30s.
# El transcript se trae solo al abrir el detalle de un candidato.
CAMPOS_TABLA = (
    "id,sheet_row,name,email,video_source,video_url,"
    "written_status,written_score,written_explanation,"
    "video_status,video_score,video_explanation,"
    "iq_status,iq_score,iq_explanation,iq_source_kind,"
    "gate1_pass,gate1_decision,gate2_pass,"
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
    candidates, counts, monitor = await asyncio.gather(
        asyncio.to_thread(
            db.list_candidates, monitor_id, limit, offset, CAMPOS_TABLA, status
        ),
        asyncio.to_thread(db.count_candidates, monitor_id),
        asyncio.to_thread(db.get_monitor, monitor_id),
    )
    return {
        "candidates": [_anotar_razones(c, monitor) for c in candidates],
        "counts": counts,
    }


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
    candidate, monitor = await asyncio.gather(
        asyncio.to_thread(db.get_candidate, candidate_id),
        asyncio.to_thread(db.get_monitor, monitor_id),
    )
    if not candidate or candidate.get("monitor_id") != monitor_id:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return _anotar_razones(candidate, monitor)


@router.post("/{monitor_id}/candidates/{candidate_id}/retry")
async def retry_candidate(monitor_id: str, candidate_id: str):
    """Reencola un candidato.

    Antes solo actuaba sobre status 'error', asi que un candidato colgado en
    'processing' no se podia recuperar desde la UI. Ahora tambien resetea
    `attempts` y el lease, que es lo que lo hace visible para la cola.

    BUG real encontrado en produccion (18 ago): si el candidato ya se habia
    sincronizado una vez al Sheet (con su estado de error), `sheet_synced_at`
    quedaba seteado para siempre. Un reintento exitoso cambiaba el puntaje en
    la base, pero el Sheet nunca se enteraba porque el candidato ya no
    calificaba para el proximo sync automatico -- la celda quedaba mostrando
    "Error" para siempre a pesar de tener nota. Resetear `sheet_synced_at` aca
    es lo que lo vuelve a poner en la fila de "hay que escribir esto".
    """
    candidate = await asyncio.to_thread(db.get_candidate, candidate_id)
    if not candidate or candidate.get("monitor_id") != monitor_id:
        raise HTTPException(status_code=404, detail="Candidate not found")

    update = {
        "error_message": None,
        "attempts": 0,
        "worker_id": None,
        "lease_expires_at": db.EPOCH,
        "sheet_synced_at": None,
    }
    if candidate.get("written_status") in ("error", "processing"):
        update["written_status"] = "pending"
    if candidate.get("video_status") in ("error", "processing"):
        update["video_status"] = "pending"
    # La etapa IQ solo se reintenta desde error/processing: un 'waiting' no es
    # trabajo trabado, es un candidato esperando que exista su sesion.
    if candidate.get("iq_status") in ("error", "processing"):
        update["iq_status"] = "pending"

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

    # La guarda de desalineacion tambien aca. Antes iba en None -- es decir,
    # SIN guarda -- y este endpoint es justo el que se aprieta cuando algo se
    # desalineo: reescribia las 300 celdas ignorando si la fila seguia siendo de
    # la misma persona. Con una fila editada a mano, le habria puesto la nota de
    # un candidato en la fila de otro. Es el error mas caro del sistema y estaba
    # a un clic.
    from tools.sheet_reader import read_all_rows

    correos_por_fila: dict[int, str] | None = None
    try:
        headers, filas = await asyncio.to_thread(
            read_all_rows, monitor["sheet_id"],
            monitor.get("worksheet_name") or "Form Responses 1",
        )
        idx = next(
            (i for i, h in enumerate(headers) if "email" in (h or "").lower()), None
        )
        if idx is not None:
            correos_por_fila = {
                n: (f[idx] if idx < len(f) else "")
                for n, f in enumerate(filas, start=2)
            }
    except Exception as e:
        # Sin poder leer la planilla no se puede verificar la alineacion, y sin
        # verificarla no se escribe: es preferible que el sync manual falle a que
        # cruce dos candidatos.
        raise HTTPException(
            status_code=503,
            detail=f"No se pudo leer la planilla para verificar la alineacion: {str(e)[:200]}",
        )

    resultado = await asyncio.to_thread(
        sync_completed_to_sheet, monitor, force, correos_por_fila
    )
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


# --- Embudo del consultor --------------------------------------------------


@router.get("/{monitor_id}/iq/sesiones")
async def iq_sesiones(monitor_id: str):
    """Matcheo EN SECO de la carpeta de grabaciones contra los candidatos.

    No evalua, no gasta LLM y no escribe nada: es para verificar, antes de
    activar la etapa, que cada archivo de la carpeta cae en el candidato que
    corresponde y que ninguna mentoria o llamada de venta guardada en la misma
    carpeta matchea con alguien.

    `elegible` dice si ese candidato hoy tomaria la sesion (Paula lo aprobo y
    esta esperando); los que no lo son aparecen igual, para poder revisar el
    matcheo sin depender del estado del embudo.
    """
    from tools.meet_recordings import listar_sesiones, matchear

    monitor = await asyncio.to_thread(db.get_monitor, monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")
    if not monitor.get("iq_recordings_folder_id"):
        raise HTTPException(
            status_code=400,
            detail="El monitor no tiene `iq_recordings_folder_id` configurado",
        )

    sesiones = await asyncio.to_thread(
        listar_sesiones,
        monitor["iq_recordings_folder_id"],
        monitor.get("iq_session_title"),
    )
    candidatos = await asyncio.to_thread(db.list_candidates_gate_state, monitor_id)
    resultado = await asyncio.to_thread(matchear, sesiones, candidatos)

    rubrica = await asyncio.to_thread(db.get_criteria_for_monitor, monitor_id, "iq")

    return {
        "archivos_de_sesion": len(sesiones),
        "rubrica_iq_confirmada": bool(rubrica and rubrica.get("confirmed")),
        "matches": [
            {
                "candidato": m["candidate"].get("name"),
                "fila": m["candidate"].get("sheet_row"),
                "fuente": m["sesion"]["kind"],
                "archivo": m["sesion"]["name"],
                "elegible": m["candidate"].get("gate1_decision") == "aprobado"
                and m["candidate"].get("iq_status") == "waiting",
                "estado": gates.estado_texto(monitor, m["candidate"]),
            }
            for m in resultado["matches"]
        ],
        "problemas": resultado["problemas"],
    }


@router.post("/{monitor_id}/gates/recompute")
async def gates_recompute(monitor_id: str):
    """Corre el ciclo de gates ahora, sin esperar el ciclo de 60 s.

    Es lo que se usa para el backfill: recalcula los cortes de los candidatos que
    ya estaban evaluados antes de que el embudo existiera, lee las decisiones que
    ya esten escritas en la planilla, avisa a Slack la primera tanda y busca las
    sesiones de los aprobados.
    """
    from tools.sheet_reader import read_all_rows

    monitor = await asyncio.to_thread(db.get_monitor, monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")

    headers, data_rows = await asyncio.to_thread(
        read_all_rows, monitor["sheet_id"], monitor.get("sheet_name", "Form Responses 1")
    )
    resumen = await asyncio.to_thread(gates.ciclo, monitor, headers, data_rows, None)
    await asyncio.to_thread(
        db.log_activity, monitor_id, "gates_recompute", f"Ciclo de gates manual: {resumen}"
    )
    return resumen
