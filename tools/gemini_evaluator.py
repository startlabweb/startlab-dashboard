import json
import os
import re
from pathlib import Path

import google.generativeai as genai

from tools.logger import get_logger

log = get_logger("gemini_evaluator")

DEFAULT_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "gemini_video_analysis.md"
EXPECTED_KEYS = {"text", "duracion_segundos", "pausas", "muletillas", "continuidad"}


def evaluate_video(file_uri: str, prompt: str | None = None) -> dict:
    """Transcribe and analyze video with Gemini.

    Args:
        file_uri: Gemini file URI
        prompt: Custom prompt. If None, uses default from prompts/gemini_video_analysis.md
    """
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])

    model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    if prompt is None:
        prompt = DEFAULT_PROMPT_PATH.read_text(encoding="utf-8")

    model = genai.GenerativeModel(model_name=model_name)
    file_ref = genai.get_file(file_uri.split("/")[-1]) if "/" in file_uri else genai.get_file(file_uri)

    log.info(f"Sending video to {model_name} for transcription...")
    response = model.generate_content([file_ref, prompt])

    raw = response.text.strip()
    log.debug(f"Gemini response (first 500 chars): {raw[:500]}")

    # Clean markdown fences
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', cleaned)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        try:
            fixed = re.sub(r'(?<=: ")(.*?)(?=")', lambda m: m.group(0).replace('\n', '\\n'), cleaned, flags=re.DOTALL)
            data = json.loads(fixed)
        except json.JSONDecodeError:
            log.warning("Broken JSON, attempting manual extraction...")
            text_match = re.search(r'"text"\s*:\s*"(.*?)"(?:\s*,|\s*\})', cleaned, re.DOTALL)
            dur_match = re.search(r'"duracion_segundos"\s*:\s*(\d+)', cleaned)
            if text_match:
                data = {
                    "text": text_match.group(1).replace('\\n', '\n'),
                    "duracion_segundos": int(dur_match.group(1)) if dur_match else 0,
                    "pausas": 0,
                    "pausas_detalle": "Not available due to response format error",
                    "muletillas": {"conteo": 0, "lista": {}, "detalle": "Not available"},
                    "continuidad": {"puntaje": 3, "detalle": "Not available"},
                }
            else:
                return {"error": "Could not parse response", "raw": raw[:200]}

    missing = EXPECTED_KEYS - set(data.keys())
    if missing:
        log.warning(f"Missing keys in Gemini response: {missing}")

    log.info(f"Transcription: {len(data.get('text', ''))} chars, duration: {data.get('duracion_segundos', '?')}s")
    return data
