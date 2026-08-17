"""Pipeline processor: handles evaluation of new candidates."""

import logging
import os
from datetime import datetime, timezone
from typing import Callable

from app import database as db
from app.config import settings
from app.services.cost_tracker import record_cost
from app.worker.video_router import detect_video_source
from tools.sheet_reader import read_all_rows, detect_video_url
from tools.written_evaluator import evaluate_written_answers
from tools.drive_metadata import get_metadata
from tools.drive_downloader import download_video
from tools.loom_downloader import cleanup_download, download_loom
from tools.gemini_uploader import upload_to_gemini
from tools.gemini_evaluator import evaluate_video
from tools.gpt_evaluator import evaluate_transcript

log = logging.getLogger("worker.processor")


def _resolver_columnas(monitor: dict, headers: list[str]) -> dict:
    """Resuelve los indices de las columnas clave del sheet.

    Es la misma heuristica de siempre, extraida para poder usarla desde el ingest
    sin arrastrar el resto del procesamiento.
    """
    evaluator_type = monitor.get("evaluator_type", "sales")
    video_col_name = monitor.get("video_column", "") or ""

    # Puede haber MAS DE UNA columna de video: la del link (Drive o Loom) y una
    # eventual de "adjuntar archivo" del Form (que en la planilla aparece como
    # URL de Drive). Se juntan todas las candidatas; por fila se usa la primera
    # con contenido. Con una sola columna el comportamiento es el de siempre.
    video_col_idxs: list[int] = []
    name_col_idx = None
    email_col_idx = None

    PISTAS_VIDEO = ("video", "roleplay", "enlace", "adjunt", "archivo", "sube ", "subí")

    for i, h in enumerate(headers):
        h_lower = h.lower().strip()
        if evaluator_type == "sales":
            if video_col_name and video_col_name.lower() in h_lower:
                # la columna configurada va primera en la lista
                if i in video_col_idxs:
                    video_col_idxs.remove(i)
                video_col_idxs.insert(0, i)
            elif any(p in h_lower for p in PISTAS_VIDEO):
                if "puntaje" not in h_lower and "score" not in h_lower and i not in video_col_idxs:
                    video_col_idxs.append(i)
        if "name" in h_lower and "last" in h_lower:
            name_col_idx = i
        elif "nombre" in h_lower and "apellido" in h_lower:
            name_col_idx = i
        elif h_lower == "name and last name":
            name_col_idx = i
        if "email" in h_lower:
            email_col_idx = i

    video_col_idx = video_col_idxs[0] if video_col_idxs else None

    # Columnas donde el sistema ESCRIBE. No pueden pasarse al LLM como respuestas
    # del candidato.
    #
    # Antes habia un `break` aca que era un bug: `written_explanation_column` y
    # `video_explanation_column` valen los dos "Explicacion", asi que las dos
    # iteraciones encontraban la MISMA primera columna y cortaban. El indice de la
    # segunda "Explicacion" (la del roleplay) nunca entraba al set, y su texto
    # podia terminar pasandose a GPT como si fuera una respuesta del candidato.
    score_col_indexes: set[int] = set()
    ya_usados: set[int] = set()
    for col_name in (
        monitor.get("written_score_column"),
        monitor.get("written_explanation_column"),
        monitor.get("video_score_column"),
        monitor.get("video_explanation_column"),
    ):
        if not col_name:
            continue
        for i, h in enumerate(headers):
            if h.strip() == col_name.strip() and i not in ya_usados:
                score_col_indexes.add(i)
                ya_usados.add(i)
                break

    excluded_idxs = (
        {0, email_col_idx, name_col_idx} | set(video_col_idxs) | score_col_indexes
    )

    return {
        "video_col_idx": video_col_idx,
        "video_col_idxs": video_col_idxs,
        "name_col_idx": name_col_idx,
        "email_col_idx": email_col_idx,
        "excluded_idxs": excluded_idxs,
    }


def ingest_new_rows(monitor: dict, emit_event: Callable) -> dict:
    """FASE A — descubrir trabajo. Barata, idempotente, sin ninguna llamada a LLM.

    Lee el sheet una vez, pregunta en UNA query que filas ya existen, y hace UN
    upsert con las nuevas. Antes esto hacia una query de Supabase por fila (300
    round-trips secuenciales de ~400 ms) y llamaba a `create_candidate` con un
    insert crudo que, en carrera, tiraba excepcion y abortaba la tanda entera.

    Separar descubrir de procesar es lo que hace que `last_poll_at` vuelva a
    significar algo: este ciclo tarda segundos, no horas.
    """
    monitor_id = monitor["id"]
    sheet_id = monitor["sheet_id"]
    sheet_name = monitor.get("sheet_name", "Form Responses 1")
    evaluator_type = monitor.get("evaluator_type", "sales")

    headers, data_rows = read_all_rows(sheet_id, sheet_name)
    cols = _resolver_columnas(monitor, headers)
    video_col_idx = cols["video_col_idx"]
    name_col_idx = cols["name_col_idx"]
    email_col_idx = cols["email_col_idx"]
    excluded_idxs = cols["excluded_idxs"]
    video_col_idxs = cols["video_col_idxs"]

    ya_existen = db.existing_sheet_rows(monitor_id)

    nuevos: list[dict] = []
    email_por_fila: dict[int, str] = {}
    vacias = 0
    sin_video = 0

    def celda(row_data: list[str], idx: int | None) -> str:
        if idx is None or idx >= len(row_data):
            return ""
        return row_data[idx]

    for row_idx, row_data in enumerate(data_rows):
        sheet_row = row_idx + 2  # la fila 1 son los encabezados

        # Filas intermedias vacias: el sheet las devuelve y antes creaban
        # candidatos basura que despues fallaban.
        if not any(str(c).strip() for c in row_data):
            vacias += 1
            continue

        email_por_fila[sheet_row] = celda(row_data, email_col_idx)

        if sheet_row in ya_existen:
            continue

        if evaluator_type == "editor":
            video_url = ""
            source_type = "none"
            video_status = "no_video"
        else:
            # Primera columna de video con contenido (link pegado O adjunto del
            # Form). Si la celda trae varias URLs separadas por coma (adjuntos
            # multiples), se toma la primera.
            video_url = ""
            for idx in video_col_idxs:
                valor = celda(row_data, idx).strip()
                if valor:
                    video_url = valor.split(",")[0].strip()
                    break
            source_type, _ = detect_video_url(video_url)
            video_status = "pending" if source_type != "none" else "no_video"
            if source_type == "none" and video_url:
                sin_video += 1

        respuestas = {
            h: row_data[i]
            for i, h in enumerate(headers)
            if i < len(row_data) and row_data[i] and i not in excluded_idxs
        }

        nuevos.append(
            {
                "monitor_id": monitor_id,
                "sheet_row": sheet_row,
                "name": celda(row_data, name_col_idx),
                "email": celda(row_data, email_col_idx),
                "video_url": video_url,
                "video_source": source_type,
                "video_status": video_status,
                "written_answers": respuestas,
            }
        )

    insertados = db.upsert_candidates(nuevos)

    if insertados:
        db.log_activity(
            monitor_id, "new_response", f"{insertados} respuestas nuevas encoladas"
        )
        emit_event({"type": "new_candidate", "count": insertados})
    if sin_video:
        log.warning(
            f"Monitor {monitor_id}: {sin_video} filas con link de video no reconocido"
        )

    return {
        "total_rows": len(data_rows),
        "new": insertados,
        "empty_skipped": vacias,
        "unrecognized_video": sin_video,
        "headers": headers,
        "email_by_row": email_por_fila,
    }


def process_one(monitor: dict, candidate: dict, emit_event: Callable) -> bool:
    """FASE B — procesar UN candidato ya reclamado por la cola.

    El llamador (el drain loop) ya hizo el claim atomico, asi que aca no hay que
    volver a chequear si alguien mas lo tiene.

    Returns:
        True si el candidato quedo en un estado terminal (nada mas que hacer),
        False si conviene reintentarlo mas adelante.
    """
    monitor_id = monitor["id"]
    candidate_id = candidate["id"]
    evaluator_type = monitor.get("evaluator_type", "sales")

    # Las respuestas escritas se guardaron en el ingest; no hace falta releer el sheet.
    headers = list((candidate.get("written_answers") or {}).keys())

    if candidate.get("written_status") in ("pending", "error"):
        _process_written(
            monitor, candidate, candidate_id, headers, [], set(), emit_event
        )

    if evaluator_type == "sales":
        fresco = db.get_candidate(candidate_id) or candidate
        if fresco.get("video_status") in ("pending", "error"):
            _process_video(monitor, fresco, candidate_id, emit_event)

    final = db.get_candidate(candidate_id) or {}
    ws = final.get("written_status")
    vs = final.get("video_status")
    terminal = ws in ("completed",) and vs in ("completed", "no_video")
    return bool(terminal)


def process_new_candidates(monitor: dict, emit_event: Callable):
    """Compatibilidad: ingesta y despues procesa en serie lo que encuentre.

    El manager nuevo usa `ingest_new_rows` + `process_one` con la cola. Esta
    funcion queda para no romper ningun call site viejo.
    """
    ingest_new_rows(monitor, emit_event)
    while True:
        candidato = db.find_claimable(monitor["id"])
        if not candidato:
            break
        reclamado = db.claim_candidate(candidato)
        if not reclamado:
            continue
        ok = False
        try:
            ok = process_one(monitor, reclamado, emit_event)
        finally:
            db.release_candidate(candidate_id=reclamado["id"], ok=ok, attempts=reclamado.get("attempts", 1))


def _process_written(monitor, candidate, candidate_id, headers, row_data, excluded_idxs, emit_event):
    """Evaluate written answers."""
    monitor_id = monitor["id"]
    sheet_row = candidate["sheet_row"]
    name = candidate.get("name", "Unknown")

    written_criteria = db.get_criteria_for_monitor(monitor_id, "written")
    if not written_criteria or not written_criteria.get("confirmed"):
        return  # No criteria configured

    try:
        db.update_candidate(candidate_id, {"written_status": "processing"})
        emit_event({"type": "processing", "phase": "written", "name": name, "row": sheet_row})

        # Build answers dict
        answers = candidate.get("written_answers") or {}
        if not answers and row_data:
            for i, h in enumerate(headers):
                if i < len(row_data) and row_data[i] and i not in excluded_idxs:
                    answers[h] = row_data[i]

        result = evaluate_written_answers(
            answers=answers,
            prompt_template=written_criteria["gpt_prompt_template"],
            candidate_info={"row_number": sheet_row, "name": name},
        )

        if "error" in result:
            db.update_candidate(candidate_id, {
                "written_status": "error",
                "error_message": result["error"],
            })
            emit_event({"type": "error", "phase": "written", "name": name, "error": result["error"]})
            return

        score = result.get("puntuacion_total", 0)
        explanation = result.get("resumen", "")

        db.update_candidate(candidate_id, {
            "written_status": "completed",
            "written_score": score,
            "written_breakdown": result,
            "written_explanation": explanation,
        })

        record_cost(monitor_id, candidate_id, 0.02)
        db.log_activity(monitor_id, "written_complete", f"{name}: written {score}/{written_criteria['total_points']}")
        emit_event({"type": "written_complete", "name": name, "score": score, "total": written_criteria["total_points"]})

        # La escritura al Sheet la hace sheet_sync en lote. Escribir aca, una fila
        # por candidato, era lo que generaba ~2.400 requests y los 429.

    except Exception as e:
        error_msg = str(e)
        log.error(f"Written eval error row {sheet_row}: {error_msg}")
        db.update_candidate(candidate_id, {"written_status": "error", "error_message": error_msg})
        emit_event({"type": "error", "phase": "written", "name": name, "error": error_msg})

        # El error tambien lo escribe sheet_sync: escribe "Error: <mensaje>" para
        # los status 'error'. Que las 300 filas tengan ALGO el martes importa —
        # una celda vacia es indistinguible de "no se proceso".


def _process_video(monitor, candidate, candidate_id, emit_event):
    """Evaluate video roleplay."""
    monitor_id = monitor["id"]
    sheet_row = candidate["sheet_row"]
    name = candidate.get("name", "Unknown")
    video_source = candidate.get("video_source", "none")
    video_url = candidate.get("video_url", "")

    if video_source == "none":
        db.update_candidate(candidate_id, {"video_status": "no_video"})
        return

    video_criteria = db.get_criteria_for_monitor(monitor_id, "video")
    if not video_criteria or not video_criteria.get("confirmed"):
        return  # No video criteria configured

    local_path = None
    try:
        db.update_candidate(candidate_id, {"video_status": "processing"})
        emit_event({"type": "processing", "phase": "video", "name": name, "row": sheet_row})

        # Download video
        if video_source == "google_drive":
            source_type, file_id = detect_video_source(video_url)
            meta = get_metadata(file_id)
            if not meta.get("accessible"):
                # Mensaje accionable: es el error mas comun del intake por link
                # (en la edicion pasada, ~2 de cada 5 links venian sin permiso).
                raise RuntimeError(
                    "Video sin acceso: pedirle al candidato que comparta el archivo "
                    "como 'Cualquier persona con el enlace' (no una carpeta) y usar "
                    f"Reintentar. Detalle: {meta.get('error', 'desconocido')}"
                )
            if meta.get("too_large"):
                raise RuntimeError(
                    f"El video pesa {meta.get('size_mb')} MB y el maximo es "
                    f"{os.environ.get('MAX_VIDEO_SIZE_MB', 500)} MB. Pedirle al "
                    "candidato que lo comprima o lo re-grabe mas corto, y Reintentar."
                )
            local_path = download_video(file_id, candidate_id)
        else:  # loom
            # job_key = candidate_id: el aislamiento real lo da el directorio con
            # uuid del downloader, esto es para poder rastrearlo en los logs.
            local_path = download_loom(video_url, candidate_id)

        emit_event({"type": "downloaded", "name": name})

        # Transcribir + medir fluidez. Los dos motores devuelven la MISMA
        # estructura, asi que el prompt de scoring y la rubrica no cambian.
        #
        # Fallback automatico: en el test de volumen, 1 de los 4 Looms reales
        # devolvia transcripcion VACIA en AssemblyAI (audio raro) y quemaba sus
        # 3 intentos hasta quedar en error. Gemini si le sacaba transcripcion.
        # Si el titular no puede con un audio, se intenta el otro motor en el
        # MISMO intento, y queda registrado en la actividad.
        gemini_data = None
        if settings.TRANSCRIBER == "assembly":
            from tools.assembly_transcriber import transcribe as assembly_transcribe

            try:
                gemini_data = assembly_transcribe(local_path)
            except Exception as e_asm:
                log.warning(
                    f"Row {sheet_row}: AssemblyAI fallo ({str(e_asm)[:120]}); "
                    f"cayendo a Gemini en el mismo intento"
                )
                db.log_activity(
                    monitor_id,
                    "fallback_gemini",
                    f"{name} (fila {sheet_row}): AssemblyAI no pudo con el audio, se uso Gemini",
                )
                gemini_data = None

        if gemini_data is None:
            file_uri = upload_to_gemini(local_path)
            gemini_data = evaluate_video(file_uri)

        if "error" in gemini_data:
            raise RuntimeError(f"Error de transcripcion: {gemini_data['error']}")

        # Un transcript vacio NO se puntua. Medido en el ensayo: un audio que
        # devolvio texto vacio recibio 8/20 con 6/6 en fluidez — mas nota que un
        # roleplay real. Una nota calculada sobre la nada es peor que un error,
        # porque parece perfectamente valida en la planilla.
        transcripcion = (gemini_data.get("text") or "").strip()
        if len(transcripcion) < 50:
            raise RuntimeError(
                f"Transcripcion vacia o demasiado corta ({len(transcripcion)} "
                f"caracteres): no se puede evaluar el roleplay"
            )

        emit_event({"type": "transcribed", "name": name, "duration": gemini_data.get("duracion_segundos", 0)})

        # Evaluate with GPT
        gpt_result = evaluate_transcript(
            gemini_data=gemini_data,
            candidate_info={"row_number": sheet_row, "name": name},
            prompt_template=video_criteria["gpt_prompt_template"],
        )

        if "error" in gpt_result:
            raise RuntimeError(f"GPT error: {gpt_result['error']}")

        score = gpt_result.get("puntuacion_total", 0)
        explanation = gpt_result.get("resumen", "")

        # Si la transcripcion no es confiable, el aviso tiene que llegar hasta la
        # planilla: 6 de los 20 puntos (fluidez) se calculan sobre los datos de
        # Gemini, y si esos datos no son medidos la nota no se puede defender.
        calidad = gemini_data.get("calidad") or {}
        if calidad.get("fluidez_estimada") or calidad.get("transcripcion_sospechosa"):
            motivos = "; ".join(calidad.get("motivos") or ["sin detalle"])
            explanation = f"⚠ REVISAR A MANO — {motivos}. {explanation}"
            log.warning(f"Row {sheet_row} marcado para revision: {motivos}")
            db.log_activity(
                monitor_id,
                "revisar_a_mano",
                f"{name} (fila {sheet_row}): {motivos}",
            )

        db.update_candidate(candidate_id, {
            "video_status": "completed",
            "video_score": score,
            "video_breakdown": gpt_result,
            "video_explanation": explanation,
            "transcript": gemini_data.get("text", ""),
            "processed_at": datetime.now(timezone.utc).isoformat(),
        })

        cost = 0.15 + 0.02  # Gemini + GPT
        record_cost(monitor_id, candidate_id, cost)
        db.log_activity(monitor_id, "video_complete", f"{name}: video {score}/{video_criteria['total_points']} — Cost: ${cost:.2f}")
        emit_event({"type": "video_complete", "name": name, "score": score, "total": video_criteria["total_points"], "cost": cost})

        # Idem: la escritura la hace sheet_sync en lote.

    except Exception as e:
        error_msg = str(e)
        log.error(f"Video eval error row {sheet_row}: {error_msg}")
        db.update_candidate(candidate_id, {"video_status": "error", "error_message": error_msg})
        emit_event({"type": "error", "phase": "video", "name": name, "error": error_msg})

        # Idem: sheet_sync escribe "Error: <mensaje>" en la fila.

    finally:
        # Ambos caminos (Drive y Loom) usan directorios con uuid bajo TMP_ROOT:
        # se borra el directorio completo. Restos acumulados = disco lleno (5 GB).
        if local_path is not None:
            cleanup_download(local_path)
