import os
import socket
from datetime import datetime, timedelta, timezone

from supabase import create_client, Client
from app.config import settings

_client: Client | None = None

# Identifica a este proceso en la cola. Si el worker corre en un servicio aparte
# de Railway, cada replica tiene su propio hostname.
WORKER_ID = os.getenv("WORKER_ID") or f"{socket.gethostname()}:{os.getpid()}"

# Sentinela de "lease libre". Coincide con el default de la migracion 003.
EPOCH = "1970-01-01T00:00:00+00:00"


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


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


def list_candidates(
    monitor_id: str,
    limit: int = 100,
    offset: int = 0,
    campos: str = "*",
    status: str | None = None,
) -> list[dict]:
    """Lista candidatos en el mismo orden que el Sheet (`sheet_row` ascendente).

    `campos` permite pedir una projection angosta: traer select("*") con 300 filas
    arrastra `transcript` y `written_answers` completos (varios MB por request).
    `status` filtra por estado en cualquiera de las dos fases.

    Se trae TODO (paginado de a 1000, mismo patron que `existing_sheet_rows`) para
    poder calcular `total_score` de cada fila en Python: el frontend lo usa para
    los badges de top 5/suplente y los filtros por puntaje. Con los cientos de
    candidatos de una convocatoria esto es barato: es la misma projection angosta
    que ya se usaba, solo que sin el limit aplicado en la query.
    """
    db = get_db()
    campos_query = campos
    if campos != "*":
        requeridos = {
            "written_score",
            "video_score",
            "written_status",
            "video_status",
            "iq_status",
        }
        faltantes = requeridos - set(c.strip() for c in campos.split(","))
        if faltantes:
            campos_query = campos + "," + ",".join(faltantes)

    filas: list[dict] = []
    paso = 1000
    desde = 0
    while True:
        q = db.table("candidates").select(campos_query).eq("monitor_id", monitor_id)
        if status:
            q = q.or_(
                f"written_status.eq.{status},video_status.eq.{status},"
                f"iq_status.eq.{status}"
            )
        lote = q.order("sheet_row", desc=False).range(desde, desde + paso - 1).execute().data or []
        filas.extend(lote)
        if len(lote) < paso:
            break
        desde += paso

    def total_de(c: dict) -> float | None:
        # Misma regla que list_completed_for_total: un total solo tiene sentido
        # cuando las DOS fases terminaron. Antes de eso queda None (el frontend
        # lo muestra como "-"), no un 0 enganoso.
        if c.get("written_status") == "completed" and c.get("video_status") in (
            "completed",
            "no_video",
        ):
            return (c.get("written_score") or 0) + (c.get("video_score") or 0)
        return None

    for c in filas:
        c["total_score"] = total_de(c)

    # Mismo orden que el Sheet (pedido de Jossy, 19 ago 2026): por fila, o sea
    # orden de llegada. El ranking por puntaje se hace en el frontend via
    # filtros, no reordenando la lista -- asi la tabla del monitor y la planilla
    # se leen igual, fila por fila.
    filas.sort(key=lambda c: c["sheet_row"])

    return filas[offset : offset + limit]


def update_candidate(candidate_id: str, data: dict) -> dict:
    db = get_db()
    result = db.table("candidates").update(data).eq("id", candidate_id).execute()
    return result.data[0] if result.data else {}


def _counts_vacios() -> dict:
    return {"total": 0, "completed": 0, "processing": 0, "pending": 0, "error": 0}


# Estados de la etapa IQ que NO son trabajo pendiente: 'waiting' es "aprobado y
# esperando que exista la sesion" y 'no_session' es "no va a tener sesion". Un
# candidato asi esta terminado desde el punto de vista del worker.
IQ_SIN_TRABAJO = ("waiting", "no_session", "completed")


def _sumar_a_counts(counts: dict, row: dict):
    """Clasifica una fila de candidato en el bucket que le corresponde."""
    counts["total"] += 1
    # Consider completed if written is done (video might be no_video)
    ws = row.get("written_status", "pending")
    vs = row.get("video_status", "pending")
    iq = row.get("iq_status", "waiting")
    if ws == "completed" and vs in ("completed", "no_video") and iq in IQ_SIN_TRABAJO:
        counts["completed"] += 1
    elif ws == "error" or vs == "error" or iq == "error":
        counts["error"] += 1
    elif ws == "processing" or vs == "processing" or iq == "processing":
        counts["processing"] += 1
    else:
        counts["pending"] += 1


def count_candidates_bulk(monitor_ids: list[str]) -> dict[str, dict]:
    """Conteos por status de varios monitores en UNA sola query.

    Antes se hacia una query por monitor (N+1): con 3 monitores eran 3 viajes
    secuenciales a Supabase y el endpoint tardaba 7-22s.
    """
    counts = {mid: _counts_vacios() for mid in monitor_ids}
    if not monitor_ids:
        return counts

    db = get_db()
    all_rows = (
        db.table("candidates")
        .select("monitor_id, written_status, video_status, iq_status")
        .in_("monitor_id", monitor_ids)
        .execute()
    )
    for row in all_rows.data:
        c = counts.get(row.get("monitor_id"))
        if c is None:
            continue
        _sumar_a_counts(c, row)
    return counts


def count_candidates(monitor_id: str) -> dict:
    """Returns counts by status."""
    return count_candidates_bulk([monitor_id])[monitor_id]


# --- Cola con lease ---------------------------------------------------------
#
# La tabla `candidates` ES la cola: no hay tabla de jobs aparte, asi que el estado
# del candidato y el estado de la cola son lo mismo y no hay que reconciliar dos
# fuentes de verdad. Requiere la migracion 003.

# Un candidato es reclamable si alguna de sus fases no termino. 'processing'
# entra a proposito: si el worker murio, el lease vencido lo devuelve a la cola.
#
# `iq_status` suma solo 'pending', 'processing' y 'error'. NI 'waiting' NI
# 'no_session' entran, y eso es lo que hace que la tercera etapa no rompa la
# cola: un candidato aprobado que todavia no tuvo su sesion de IQ esta en
# 'waiting', y si 'waiting' fuera reclamable se lo tomaria en cada ciclo hasta
# quemar sus 3 intentos sin que exista nada que evaluar.
CLAIMABLE = (
    "written_status.in.(pending,processing,error),"
    "video_status.in.(pending,processing,error),"
    "iq_status.in.(pending,processing,error)"
)


def find_claimable(monitor_id: str, max_attempts: int = 3) -> dict | None:
    """Devuelve el candidato pendiente mas viejo con el lease libre. NO lo reserva.

    Reservarlo es el trabajo de `claim_candidate`: separar las dos cosas permite
    que el claim sea un compare-and-swap sobre una fila concreta.
    """
    db = get_db()
    result = (
        db.table("candidates")
        .select("*")
        .eq("monitor_id", monitor_id)
        .lt("attempts", max_attempts)
        .lt("lease_expires_at", _iso(_ahora()))
        .or_(CLAIMABLE)
        .order("sheet_row", desc=False)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def claim_candidate(candidate: dict, lease_seconds: int = 1800) -> dict | None:
    """Reserva el candidato con un compare-and-swap. Devuelve la fila o None.

    None significa que otro worker lo tomo primero: hay que seguir al siguiente,
    no reintentar. Un solo UPDATE ... WHERE en Postgres es atomico, asi que dos
    contenedores solapados (lo normal durante un deploy de Railway) no pueden
    ganar el mismo candidato. Esto es lo que hace innecesario FOR UPDATE SKIP
    LOCKED, que PostgREST no puede expresar.

    `attempts` se incrementa ACA, no al fallar: un OOM o un SIGKILL no ejecutan
    ningun except, y si el contador solo subiera al fallar, un video que mata al
    contenedor se reintentaria para siempre y bloquearia la cola.

    lease_seconds=1800 es el techo del peor caso, no un parametro de tuning:
    yt-dlp 240s + Gemini upload hasta 300s + transcripcion + GPT, con margen. Un
    lease corto hace que el reaper le robe el trabajo a un worker que esta
    trabajando bien, y eso cuesta plata (doble llamada a Gemini).
    """
    db = get_db()
    previos = candidate.get("attempts", 0)
    ahora = _ahora()
    result = (
        db.table("candidates")
        .update(
            {
                "attempts": previos + 1,
                "worker_id": WORKER_ID,
                "started_at": _iso(ahora),
                "lease_expires_at": _iso(ahora + timedelta(seconds=lease_seconds)),
            }
        )
        .eq("id", candidate["id"])
        .eq("attempts", previos)                    # <- CAS
        .lt("lease_expires_at", _iso(ahora))        # <- el lease seguia libre
        .execute()
    )
    return result.data[0] if result.data else None


def release_candidate(candidate_id: str, ok: bool, attempts: int) -> None:
    """Libera el lease al terminar de procesar.

    ok=True  -> lease a epoch. El status ya excluye al candidato de la cola.
    ok=False -> lease = ahora + backoff exponencial (2, 4, 8 min). El mismo campo
                hace de next_attempt_at, asi que no hace falta otra columna.
    """
    if ok:
        proximo = EPOCH
    else:
        espera = min(600, 60 * (2 ** max(0, attempts)))
        proximo = _iso(_ahora() + timedelta(seconds=espera))

    get_db().table("candidates").update(
        {"lease_expires_at": proximo, "worker_id": None}
    ).eq("id", candidate_id).execute()


def reap_expired_leases(monitor_id: str, max_attempts: int = 3) -> int:
    """Cuenta los candidatos con lease vencido (los huerfanos de un crash).

    No hace falta modificarlos: `find_claimable` ya los ve, porque su condicion es
    lease vencido y no status. Esta funcion existe solo para poder reportarlos en
    /progress — un numero sostenido > 0 significa que el worker esta muerto.
    """
    db = get_db()
    result = (
        db.table("candidates")
        .select("id")
        .eq("monitor_id", monitor_id)
        .lt("attempts", max_attempts)
        .lt("lease_expires_at", _iso(_ahora()))
        .or_(
            "written_status.eq.processing,video_status.eq.processing,"
            "iq_status.eq.processing"
        )
        .execute()
    )
    return len(result.data or [])


def existing_sheet_rows(monitor_id: str) -> set[int]:
    """Filas del sheet que ya estan en la base. UNA query.

    Reemplaza los ~300 `get_candidate_by_row` que el processor hacia por ciclo
    (uno por fila, secuenciales, ~400 ms cada uno).
    """
    db = get_db()
    filas: set[int] = set()
    paso = 1000
    offset = 0
    while True:
        result = (
            db.table("candidates")
            .select("sheet_row")
            .eq("monitor_id", monitor_id)
            .range(offset, offset + paso - 1)
            .execute()
        )
        lote = result.data or []
        filas.update(r["sheet_row"] for r in lote)
        if len(lote) < paso:
            break
        offset += paso
    return filas


def list_candidates_for_realign(monitor_id: str) -> list[dict]:
    """id, sheet_row, email y name de TODOS los candidatos del monitor.

    Alimenta la realineacion por email del ingest: es la foto contra la que
    se compara la posicion real de cada email en el sheet.
    """
    db = get_db()
    out: list[dict] = []
    paso = 1000
    offset = 0
    while True:
        result = (
            db.table("candidates")
            .select("id,sheet_row,email,name")
            .eq("monitor_id", monitor_id)
            .range(offset, offset + paso - 1)
            .execute()
        )
        lote = result.data or []
        out.extend(lote)
        if len(lote) < paso:
            break
        offset += paso
    return out


def move_sheet_rows(moves: list[dict]) -> None:
    """Reubica candidatos a nuevas filas en 2 pasadas batcheadas.

    La pasada intermedia (+100000) esquiva el UNIQUE(monitor_id, sheet_row)
    cuando dos candidatos intercambian posiciones. `moves` es una lista de
    {"id": ..., "sheet_row": fila_final}.
    """
    if not moves:
        return
    db = get_db()
    paso1 = [{"id": m["id"], "sheet_row": m["sheet_row"] + 100000} for m in moves]
    db.table("candidates").upsert(paso1).execute()
    paso2 = [{"id": m["id"], "sheet_row": m["sheet_row"]} for m in moves]
    db.table("candidates").upsert(paso2).execute()


def retry_all_unfinished(monitor_id: str) -> int:
    """Reencola todo lo que no llego a un estado terminal. Devuelve cuantos.

    Resetea `attempts`, el lease y `sheet_synced_at`. Esto ultimo es critico:
    sin resetearlo, un candidato que ya se sincronizo una vez (con su estado
    de error) queda invisible para el proximo sync automatico aunque el
    reintento termine bien -- la celda del Sheet se queda mostrando "Error"
    para siempre. Bug real encontrado en produccion el 18 ago.
    """
    db = get_db()
    pendientes = (
        db.table("candidates")
        .select("id,written_status,video_status,iq_status")
        .eq("monitor_id", monitor_id)
        .or_(CLAIMABLE)
        .execute()
        .data
        or []
    )
    if not pendientes:
        return 0

    ids = [c["id"] for c in pendientes]
    for i in range(0, len(ids), 200):
        lote = ids[i : i + 200]
        db.table("candidates").update(
            {
                "attempts": 0,
                "worker_id": None,
                "lease_expires_at": EPOCH,
                "error_message": None,
                "sheet_synced_at": None,
            }
        ).in_("id", lote).execute()

    # 'processing' y 'error' vuelven a 'pending' por fase, sin tocar lo completado.
    # `iq_status` se incluye, pero solo desde 'processing'/'error': un 'waiting'
    # no es trabajo trabado, es un candidato esperando que exista su sesion.
    for campo in ("written_status", "video_status", "iq_status"):
        for estado in ("processing", "error"):
            afectados = [c["id"] for c in pendientes if c.get(campo) == estado]
            for i in range(0, len(afectados), 200):
                lote = afectados[i : i + 200]
                if lote:
                    db.table("candidates").update({campo: "pending"}).in_(
                        "id", lote
                    ).execute()

    return len(ids)


def monitor_progress(monitor_id: str) -> dict | None:
    """Agregado de progreso. Se calcula en Python sobre una projection angosta.

    Sin RPC a proposito: con 300 filas y 6 columnas el costo es despreciable, y
    evita depender de plpgsql.
    """
    if not get_monitor(monitor_id):
        return None

    db = get_db()
    filas = (
        db.table("candidates")
        .select(
            "written_status,video_status,iq_status,attempts,lease_expires_at,"
            "sheet_synced_at,processed_at,cost_usd"
        )
        .eq("monitor_id", monitor_id)
        .execute()
        .data
        or []
    )

    ahora = _ahora()
    ahora_iso = _iso(ahora)
    hace_15 = _iso(ahora - timedelta(minutes=15))

    TERMINAL_W = ("completed",)
    TERMINAL_V = ("completed", "no_video")
    TERMINAL_IQ = IQ_SIN_TRABAJO

    total = len(filas)
    done = 0
    en_vuelo = 0
    pendientes = 0
    con_error = 0
    agotados = 0
    lease_vencido = 0
    sheet_dirty = 0
    reintentando = 0
    done_15m = 0
    costo = 0.0

    for f in filas:
        ws = f.get("written_status")
        vs = f.get("video_status")
        iq = f.get("iq_status") or "waiting"
        intentos = f.get("attempts") or 0
        costo += float(f.get("cost_usd") or 0)

        if ws in TERMINAL_W and vs in TERMINAL_V and iq in TERMINAL_IQ:
            done += 1
        elif ws == "processing" or vs == "processing" or iq == "processing":
            en_vuelo += 1
            if (f.get("lease_expires_at") or "") < ahora_iso:
                lease_vencido += 1
        else:
            pendientes += 1

        if ws == "error" or vs == "error" or iq == "error":
            con_error += 1
            if intentos >= 3:
                agotados += 1
        if intentos > 1:
            reintentando += 1

        # Terminal (incluye error) pero todavia no escrito en el Sheet
        if not f.get("sheet_synced_at") and (
            ws in ("completed", "error")
            or vs in ("completed", "error")
            or iq in ("completed", "error")
        ):
            sheet_dirty += 1

        if (f.get("processed_at") or "") > hace_15:
            done_15m += 1

    por_minuto = done_15m / 15 if done_15m else 0
    faltan = total - done
    eta_min = round(faltan / por_minuto) if por_minuto > 0 and faltan > 0 else None

    return {
        "total": total,
        "done": done,
        "in_flight": en_vuelo,
        "pending": pendientes,
        "error": con_error,
        "exhausted": agotados,
        "lease_stale": lease_vencido,
        "sheet_dirty": sheet_dirty,
        "retrying": reintentando,
        "done_last_15m": done_15m,
        "eta_minutes": eta_min,
        "cost_usd": round(costo, 2),
    }


def queue_state(monitor_id: str, max_attempts: int = 3) -> dict:
    """Estado crudo de la cola para diagnosticar por curl, sin la UI."""
    db = get_db()
    filas = (
        db.table("candidates")
        .select(
            "sheet_row,written_status,video_status,iq_status,attempts,"
            "lease_expires_at,worker_id"
        )
        .eq("monitor_id", monitor_id)
        .execute()
        .data
        or []
    )
    ahora_iso = _iso(_ahora())
    reclamables = [
        f
        for f in filas
        if (f.get("attempts") or 0) < max_attempts
        and (f.get("lease_expires_at") or "") < ahora_iso
        and (
            f.get("written_status") in ("pending", "processing", "error")
            or f.get("video_status") in ("pending", "processing", "error")
            or f.get("iq_status") in ("pending", "processing", "error")
        )
    ]
    en_vuelo = [f for f in filas if (f.get("lease_expires_at") or "") >= ahora_iso]
    return {
        "total": len(filas),
        "claimable_now": len(reclamables),
        "leased_now": len(en_vuelo),
        "exhausted": sum(1 for f in filas if (f.get("attempts") or 0) >= max_attempts),
        "workers_seen": sorted({f["worker_id"] for f in filas if f.get("worker_id")}),
        "next_rows": [f["sheet_row"] for f in sorted(reclamables, key=lambda x: x["sheet_row"])[:10]],
    }


def list_completed_for_total(monitor_id: str) -> list[dict]:
    """Candidatos con las DOS fases completas, para (re)calcular 'Puntaje total'.

    A proposito NO filtra por sheet_synced_at: el total puede quedar pendiente
    de escribir si las escritas se sincronizaron en un ciclo y el video recien
    termino en el siguiente. Recalcularlo cada ciclo es barato (una sola
    llamada batcheada al Sheet) y evita ese caso.
    """
    db = get_db()
    return (
        db.table("candidates")
        .select("id,sheet_row,email,written_score,video_score")
        .eq("monitor_id", monitor_id)
        .eq("written_status", "completed")
        .in_("video_status", ["completed", "no_video"])
        .execute()
        .data
        or []
    )


def list_candidates_for_sheet_sync(monitor_id: str, force: bool = False) -> list[dict]:
    """Candidatos en estado terminal que hay que escribir al Sheet.

    force=True devuelve todos los terminales, ignorando `sheet_synced_at`. Es lo
    que respalda `POST /sync-sheet?force=true`: reconstruye las 300 celdas desde
    la base en 2 requests.
    """
    campos = (
        "id,sheet_row,email,sheet_synced_at,error_message,"
        "written_status,written_score,written_explanation,"
        "video_status,video_score,video_explanation,"
        "iq_status,iq_score,iq_explanation,"
        "gate1_pass,gate1_decision,gate2_pass"
    )
    db = get_db()
    q = (
        db.table("candidates")
        .select(campos)
        .eq("monitor_id", monitor_id)
        .or_(
            "written_status.in.(completed,error),"
            "video_status.in.(completed,error),"
            "iq_status.in.(completed,error)"
        )
    )
    if not force:
        q = q.is_("sheet_synced_at", "null")
    return q.order("sheet_row", desc=False).execute().data or []


def mark_sheet_synced(candidate_ids: list[str], chunk: int = 200) -> None:
    """Marca los candidatos como ya escritos en el Sheet. Un request por chunk."""
    if not candidate_ids:
        return
    db = get_db()
    ahora = _iso(_ahora())
    for i in range(0, len(candidate_ids), chunk):
        lote = candidate_ids[i : i + chunk]
        db.table("candidates").update({"sheet_synced_at": ahora}).in_("id", lote).execute()


def upsert_candidates(rows: list[dict], chunk: int = 100) -> int:
    """Inserta candidatos nuevos ignorando los que ya existen. Devuelve cuantos entraron.

    `ignore_duplicates=True` sobre el UNIQUE(monitor_id, sheet_row) que ya trae la
    migracion 001: NUNCA pisa el estado de una fila existente, y una carrera entre
    dos descubridores deja de tirar excepcion. Antes, `create_candidate` era un
    insert crudo y esa excepcion subia hasta el poll loop, abortando la tanda
    entera a mitad de camino.
    """
    if not rows:
        return 0
    db = get_db()
    insertados = 0
    for i in range(0, len(rows), chunk):
        lote = rows[i : i + chunk]
        result = (
            db.table("candidates")
            .upsert(lote, on_conflict="monitor_id,sheet_row", ignore_duplicates=True)
            .execute()
        )
        insertados += len(result.data or [])
    return insertados


# --- Embudo: gates y etapa IQ ----------------------------------------------


def list_candidates_gate_state(monitor_id: str) -> list[dict]:
    """Todo lo que el ciclo de gates necesita, en UNA query paginada.

    Las cuatro cosas que hace ese ciclo (calcular los cortes, leer la decision de
    Paula, avisar a Slack y buscar la sesion en Drive) miran los mismos
    candidatos, asi que se traen una sola vez por ciclo. `iq_breakdown` viene
    porque el Gate 2 sale de sus dos booleanos.
    """
    campos = (
        "id,sheet_row,name,email,video_url,error_message,"
        "written_status,written_score,video_status,video_score,"
        "iq_status,iq_score,iq_breakdown,iq_source_file_id,"
        # `iq_invited_at`, `iq_session_token` e `iq_slot_at` NO son opcionales:
        # el ciclo de gates decide con ellos si mandar la invitacion y con que
        # link. Cuando faltaban, `c.get(...)` devolvia None siempre -- el
        # candidato figuraba "sin invitar" en cada ciclo y recibio un correo por
        # minuto, cada uno con un token nuevo que invalidaba el anterior.
        # Un campo que la logica lee y la consulta no trae es un bug silencioso.
        "iq_invited_at,iq_session_token,iq_slot_at,"
        "gate1_pass,gate1_decision,gate1_notified_at,gate2_pass"
    )
    db = get_db()
    out: list[dict] = []
    paso = 1000
    offset = 0
    while True:
        lote = (
            db.table("candidates")
            .select(campos)
            .eq("monitor_id", monitor_id)
            .order("sheet_row", desc=False)
            .range(offset, offset + paso - 1)
            .execute()
            .data
            or []
        )
        out.extend(lote)
        if len(lote) < paso:
            break
        offset += paso
    return out


def mark_gate1_notified(candidate_ids: list[str], chunk: int = 200) -> None:
    """Sella el aviso de Slack para que no se repita en el proximo ciclo."""
    if not candidate_ids:
        return
    db = get_db()
    ahora = _iso(_ahora())
    for i in range(0, len(candidate_ids), chunk):
        lote = candidate_ids[i : i + chunk]
        db.table("candidates").update({"gate1_notified_at": ahora}).in_(
            "id", lote
        ).execute()


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
