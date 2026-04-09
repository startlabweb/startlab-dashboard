import json
import os
import re
from pathlib import Path

from openai import OpenAI

from tools.logger import get_logger

log = get_logger("gpt_evaluator")

DEFAULT_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "gpt_evaluation.md"


def _build_prompt(gemini_data: dict, template: str) -> str:
    """Fill placeholders in the prompt template with Gemini analysis data."""
    muletillas = gemini_data.get("muletillas", {})
    lista = muletillas.get("lista", {})
    continuidad = gemini_data.get("continuidad", {})

    replacements = {
        "{duracion_segundos}": str(gemini_data.get("duracion_segundos", 0)),
        "{text}": gemini_data.get("text", ""),
        "{pausas}": str(gemini_data.get("pausas", 0)),
        "{pausas_detalle}": gemini_data.get("pausas_detalle", ""),
        "{muletillas_conteo}": str(muletillas.get("conteo", 0)),
        "{muletillas_ehm}": str(lista.get("ehm", 0)),
        "{muletillas_este}": str(lista.get("este", 0)),
        "{muletillas_osea}": str(lista.get("o sea", 0)),
        "{muletillas_bueno}": str(lista.get("bueno", 0)),
        "{muletillas_como}": str(lista.get("como", 0)),
        "{muletillas_entonces}": str(lista.get("entonces", 0)),
        "{muletillas_detalle}": muletillas.get("detalle", ""),
        "{continuidad_puntaje}": str(continuidad.get("puntaje", 0)),
        "{continuidad_detalle}": continuidad.get("detalle", ""),
    }

    prompt = template
    for placeholder, value in replacements.items():
        prompt = prompt.replace(placeholder, value)

    return prompt


def evaluate_transcript(gemini_data: dict, candidate_info: dict, prompt_template: str | None = None) -> dict:
    """Evaluate transcript with GPT-4o.

    Args:
        gemini_data: Output from gemini_evaluator
        candidate_info: Dict with row_number, name, etc.
        prompt_template: Custom prompt template. If None, uses default.
    """
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    if prompt_template is None:
        prompt_template = DEFAULT_PROMPT_PATH.read_text(encoding="utf-8")

    prompt = _build_prompt(gemini_data, prompt_template)

    row = candidate_info.get("row_number", "?")
    log.info(f"Evaluating row {row} with GPT-4o...")
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.2,
    )

    raw = response.choices[0].message.content.strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as e:
            log.error(f"GPT did not return valid JSON: {e}")
            return {"error": str(e), "raw": raw}

    total = data.get("puntuacion_total", -1)
    log.info(f"Row {row}: {total} pts — {data.get('resumen', '')[:80]}")
    return data
