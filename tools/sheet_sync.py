"""Sincronizacion batcheada de resultados al Google Sheet.

El entregable es el Sheet, pero la fuente de verdad es Postgres. Este modulo es
el puente, y esta hecho para que ese puente se pueda reconstruir en 2 requests
en cualquier momento — incluido el martes a la mañana si algo salio mal.

Por que existe: antes cada candidato escribia al Sheet dos veces (una por fase),
inline, apenas terminaba. Con ~4 requests por llamada eso eran ~2.400 requests
para 300 candidatos, con picos de 80/min contra un limite de 60. Y si el
contenedor moria entre la evaluacion y la escritura, el resultado quedaba en la
base y nunca aparecia en el Sheet, sin que nadie se enterara.

Ahora `sheet_synced_at` es el buffer: sobrevive reinicios, es consultable, y
alimenta el `sheet_dirty` de /progress.
"""

from app import database as db
from tools.logger import get_logger
from tools.sheet_writer import write_results

log = get_logger("sheet_sync")

# Google acepta payloads grandes pero no infinitos, y 300 explicaciones largas
# pueden sumar varios MB. Se cortan las explicaciones y se pagina el flush.
MAX_EXPLICACION = 4000
CHUNK_FILAS = 200


def _recortar(texto: object) -> str:
    s = "" if texto is None else str(texto)
    if len(s) > MAX_EXPLICACION:
        return s[: MAX_EXPLICACION - 3] + "..."
    return s


def sync_completed_to_sheet(
    monitor: dict,
    force: bool = False,
    email_by_row: dict[int, str] | None = None,
) -> dict:
    """Escribe al Sheet los candidatos terminales que todavia no se sincronizaron.

    Args:
        monitor: fila del monitor.
        force: reescribe TODO ignorando `sheet_synced_at`. Es el boton de panico.
        email_by_row: si viene, se saltea cualquier fila donde el email del sheet
            no coincida con el del candidato. Protege contra que alguien haya
            ordenado o insertado filas: sin esto, escribir por numero de fila le
            pondria el puntaje de un candidato a otro, en silencio.

    Returns:
        {"written": n, "video": n, "skipped": n, "requests": n}
    """
    monitor_id = monitor["id"]
    sheet_id = monitor["sheet_id"]
    worksheet = monitor.get("sheet_name", "Form Responses 1")

    candidatos = db.list_candidates_for_sheet_sync(monitor_id, force=force)
    if not candidatos:
        return {"written": 0, "video": 0, "skipped": 0, "requests": 0}

    filas_escritas: list[dict] = []
    filas_video: list[dict] = []
    ids_written: list[str] = []
    ids_video: list[str] = []
    salteados = 0

    for c in candidatos:
        fila = c["sheet_row"]

        # Guarda de desalineacion
        if email_by_row is not None:
            esperado = (email_by_row.get(fila) or "").strip().lower()
            actual = (c.get("email") or "").strip().lower()
            if esperado and actual and esperado != actual:
                salteados += 1
                log.error(
                    f"Fila {fila} desalineada: el sheet tiene '{esperado[:40]}' y la "
                    f"base '{actual[:40]}'. NO se escribe."
                )
                continue

        ws_status = c.get("written_status")
        if ws_status in ("completed", "error") and (force or not c.get("sheet_synced_at")):
            if ws_status == "error":
                puntaje = "Error"
                explicacion = f"Escritas no evaluadas: {c.get('error_message') or 'sin detalle'}"
            else:
                puntaje = c.get("written_score")
                explicacion = c.get("written_explanation")
            filas_escritas.append(
                {
                    "row_number": fila,
                    "score": "" if puntaje is None else puntaje,
                    "explanation": _recortar(explicacion),
                }
            )
            ids_written.append(c["id"])

        vs_status = c.get("video_status")
        if vs_status in ("completed", "error") and (force or not c.get("sheet_synced_at")):
            if vs_status == "error":
                puntaje = "Error"
                explicacion = f"Video no evaluado: {c.get('error_message') or 'sin detalle'}"
            else:
                puntaje = c.get("video_score")
                explicacion = c.get("video_explanation")
            filas_video.append(
                {
                    "row_number": fila,
                    "score": "" if puntaje is None else puntaje,
                    "explanation": _recortar(explicacion),
                }
            )
            ids_video.append(c["id"])

    requests = 0

    # Dos llamadas a write_results con TODAS las filas: 1 update_cells cada una.
    # `write_results` ya soportaba multiples filas; simplemente nunca se usaba asi.
    for i in range(0, len(filas_escritas), CHUNK_FILAS):
        lote = filas_escritas[i : i + CHUNK_FILAS]
        write_results(
            sheet_id=sheet_id,
            results=lote,
            worksheet_name=worksheet,
            score_column=monitor.get("written_score_column") or "Puntaje Preguntas",
            explanation_column=monitor.get("written_explanation_column") or "Explicación",
        )
        requests += 2

    for i in range(0, len(filas_video), CHUNK_FILAS):
        lote = filas_video[i : i + CHUNK_FILAS]
        write_results(
            sheet_id=sheet_id,
            results=lote,
            worksheet_name=worksheet,
            score_column=monitor.get("video_score_column") or "Puntaje Roleplay",
            explanation_column=monitor.get("video_explanation_column") or "Explicación",
        )
        requests += 2

    # Marcar sincronizado DESPUES de escribir. Si el proceso muere en el medio, el
    # proximo flush reescribe las mismas celdas con el mismo contenido: es
    # idempotente porque `update_cells` sobreescribe por coordenada, no hace append.
    sincronizados = sorted(set(ids_written) | set(ids_video))
    if sincronizados:
        db.mark_sheet_synced(sincronizados)

    resultado = {
        "written": len(filas_escritas),
        "video": len(filas_video),
        "skipped": salteados,
        "requests": requests,
    }
    if filas_escritas or filas_video or salteados:
        log.info(f"sheet_sync monitor {monitor_id}: {resultado}")
    return resultado
