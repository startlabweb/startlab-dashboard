"""Pipeline processor: handles evaluation of new candidates."""

import logging
from datetime import datetime, timezone
from typing import Callable

from app import database as db
from app.services.cost_tracker import record_cost
from app.worker.video_router import detect_video_source
from tools.sheet_reader import read_all_rows, detect_video_url
from tools.sheet_writer import write_results
from tools.written_evaluator import evaluate_written_answers
from tools.drive_metadata import get_metadata
from tools.drive_downloader import download_video
from tools.loom_downloader import download_loom
from tools.gemini_uploader import upload_to_gemini
from tools.gemini_evaluator import evaluate_video
from tools.gpt_evaluator import evaluate_transcript

log = logging.getLogger("worker.processor")


def process_new_candidates(monitor: dict, emit_event: Callable):
    """Check for new candidates in sheet and process them."""
    monitor_id = monitor["id"]
    sheet_id = monitor["sheet_id"]
    sheet_name = monitor.get("sheet_name", "Form Responses 1")
    video_col_name = monitor.get("video_column", "")

    # Read sheet
    headers, data_rows = read_all_rows(sheet_id, sheet_name)

    # Find key column indexes
    video_col_idx = None
    name_col_idx = None
    email_col_idx = None

    for i, h in enumerate(headers):
        h_lower = h.lower().strip()
        if video_col_name and video_col_name.lower() in h_lower:
            video_col_idx = i
        elif "video" in h_lower or "roleplay" in h_lower or "enlace" in h_lower:
            if "puntaje" not in h_lower and "score" not in h_lower:
                video_col_idx = video_col_idx or i
        if "name" in h_lower and "last" in h_lower:
            name_col_idx = i
        elif "nombre" in h_lower and "apellido" in h_lower:
            name_col_idx = i
        elif h_lower == "name and last name":
            name_col_idx = i
        if "email" in h_lower:
            email_col_idx = i

    # Process each row
    for row_idx, row_data in enumerate(data_rows):
        sheet_row = row_idx + 2  # 1-indexed, header is row 1

        # Skip if already in DB
        existing = db.get_candidate_by_row(monitor_id, sheet_row)
        if existing:
            # Skip if already completed or processing
            ws = existing.get("written_status", "pending")
            vs = existing.get("video_status", "pending")
            if ws in ("completed", "processing") and vs in ("completed", "processing", "no_video"):
                continue
            # Retry pending/error
            if ws not in ("pending", "error") and vs not in ("pending", "error"):
                continue
            candidate_id = existing["id"]
        else:
            # Create new candidate record
            video_url = row_data[video_col_idx] if video_col_idx is not None and video_col_idx < len(row_data) else ""
            source_type, source_id = detect_video_url(video_url)

            candidate_data = {
                "monitor_id": monitor_id,
                "sheet_row": sheet_row,
                "name": row_data[name_col_idx] if name_col_idx is not None and name_col_idx < len(row_data) else "",
                "email": row_data[email_col_idx] if email_col_idx is not None and email_col_idx < len(row_data) else "",
                "video_url": video_url,
                "video_source": source_type,
                "video_status": "pending" if source_type != "none" else "no_video",
            }

            # Store written answers
            written_answers = {}
            for i, h in enumerate(headers):
                if i < len(row_data) and row_data[i]:
                    # Skip timestamp, email, name, video, and score columns
                    if i not in (0, email_col_idx, name_col_idx, video_col_idx):
                        written_answers[h] = row_data[i]
            candidate_data["written_answers"] = written_answers

            new_candidate = db.create_candidate(candidate_data)
            candidate_id = new_candidate["id"]
            existing = new_candidate

            db.log_activity(monitor_id, "new_response", f"New response: {candidate_data['name']} (row {sheet_row})")
            emit_event({"type": "new_candidate", "name": candidate_data["name"], "row": sheet_row})

        # --- PHASE 1: Written evaluation ---
        if existing.get("written_status") in ("pending", "error"):
            _process_written(monitor, existing, candidate_id, headers, row_data, email_col_idx, name_col_idx, video_col_idx, emit_event)

        # --- PHASE 2: Video evaluation ---
        candidate = db.get_candidate(candidate_id)  # refresh
        if candidate.get("video_status") in ("pending", "error"):
            _process_video(monitor, candidate, candidate_id, emit_event)


def _process_written(monitor, candidate, candidate_id, headers, row_data, email_col_idx, name_col_idx, video_col_idx, emit_event):
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
                if i < len(row_data) and row_data[i]:
                    if i not in (0, email_col_idx, name_col_idx, video_col_idx):
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

        # Write to sheet (non-blocking — eval is already saved in DB)
        try:
            write_results(
                sheet_id=monitor["sheet_id"],
                results=[{"row_number": sheet_row, "score": score, "explanation": explanation}],
                worksheet_name=monitor.get("sheet_name", "Form Responses 1"),
                score_column=monitor.get("written_score_column", "Puntaje Preguntas"),
                explanation_column=monitor.get("written_explanation_column", "Explicación"),
            )
        except Exception as e:
            log.warning(f"Could not write written score to sheet row {sheet_row}: {e}")
            db.log_activity(monitor_id, "sheet_write_error", f"Written score for {name} saved in DB but failed to write to sheet: {e}")

    except Exception as e:
        log.error(f"Written eval error row {sheet_row}: {e}")
        db.update_candidate(candidate_id, {"written_status": "error", "error_message": str(e)})
        emit_event({"type": "error", "phase": "written", "name": name, "error": str(e)})


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
                raise RuntimeError(f"Cannot access video: {meta.get('error', 'unknown')}")
            extract_audio = meta.get("too_large", False)
            local_path = download_video(file_id, sheet_row, extract_audio=True)
        else:  # loom or other URL
            local_path = download_loom(video_url, sheet_row, extract_audio=True)

        emit_event({"type": "downloaded", "name": name})

        # Upload to Gemini
        file_uri = upload_to_gemini(local_path)

        # Transcribe
        gemini_data = evaluate_video(file_uri)
        if "error" in gemini_data:
            raise RuntimeError(f"Gemini error: {gemini_data['error']}")

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

        # Write to sheet (non-blocking — eval is already saved in DB)
        try:
            write_results(
                sheet_id=monitor["sheet_id"],
                results=[{"row_number": sheet_row, "score": score, "explanation": explanation}],
                worksheet_name=monitor.get("sheet_name", "Form Responses 1"),
                score_column=monitor.get("video_score_column", "Puntaje Roleplay"),
                explanation_column=monitor.get("video_explanation_column", "Explicación"),
            )
        except Exception as e:
            log.warning(f"Could not write video score to sheet row {sheet_row}: {e}")
            db.log_activity(monitor_id, "sheet_write_error", f"Video score for {name} saved in DB but failed to write to sheet: {e}")

    except Exception as e:
        log.error(f"Video eval error row {sheet_row}: {e}")
        db.update_candidate(candidate_id, {"video_status": "error", "error_message": str(e)})
        emit_event({"type": "error", "phase": "video", "name": name, "error": str(e)})

    finally:
        if local_path and local_path.exists():
            local_path.unlink()
