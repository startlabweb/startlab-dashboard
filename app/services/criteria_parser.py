import json
import os
import re

from openai import OpenAI

from tools.logger import get_logger

log = get_logger("criteria_parser")

SYSTEM_PROMPT_BASE = """You are an expert at parsing evaluation criteria for personnel selection processes.
The user will provide criteria that may be poorly written, informal, or in any language (Spanish or English).

Parse this into a structured JSON with this exact format:
{
  "criteria": [
    {
      "name": "short criterion name",
      "description": "what this evaluates",
      "max_points": number,
      "levels": [
        {"points": number_or_range, "description": "what earns this score"}
      ]
    }
  ],
  "total_points": number (sum of all max_points),
  "notes": "any ambiguities or assumptions you made"
}

Rules:
- Preserve the original language of the criteria
- If point values are not specified, infer reasonable ones
- If levels/thresholds are not specified, create sensible breakpoints
- Always include a "notes" field explaining any assumptions
- Return ONLY valid JSON, no markdown"""


SYSTEM_PROMPT_EDITOR_EXTRA = """

EDITOR / MULTIPLE-CHOICE MODE:
- The criteria describe a multiple-choice questionnaire where each option has a fixed point value (e.g. "Avanzado - 5 pts, Medio - 4 pts, Básico - 3 pts").
- Each question becomes ONE criterion. The options become its `levels`.
- If an option is marked "DESCARTAR" / "DISQUALIFY" / "DESCALIFICAR", encode it as a level with `points: 0` AND add `"disqualifies": true` on that level.
- max_points for the criterion is the highest numeric option (DESCARTAR options don't raise the cap).
- If a question has no explicit point values (e.g. "Portfolio link"), set max_points: 0 and skip it as informational."""


def parse_criteria(raw_text: str, criteria_type: str, evaluator_type: str = "sales") -> dict:
    """Parse raw criteria text into structured JSON using GPT-4o."""
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    if evaluator_type == "editor":
        context = "a multiple-choice selection questionnaire (editor / content manager role)"
    elif criteria_type == "written":
        context = "written form answers"
    else:
        context = "video roleplay performance"

    system_prompt = SYSTEM_PROMPT_BASE
    if evaluator_type == "editor":
        system_prompt = SYSTEM_PROMPT_BASE + SYSTEM_PROMPT_EDITOR_EXTRA

    user_msg = f"Parse the following evaluation criteria for {context}:\n\n---\n{raw_text}\n---"

    log.info(f"Parsing {criteria_type} criteria (evaluator={evaluator_type}) with GPT-4o...")
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
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
            log.error(f"Failed to parse criteria: {e}")
            return {"error": f"Could not parse criteria: {e}"}

    log.info(f"Parsed {len(data.get('criteria', []))} criteria, total: {data.get('total_points')} pts")
    return data


def generate_evaluation_prompt(
    criteria: list[dict],
    total_points: int,
    criteria_type: str,
    evaluator_type: str = "sales",
) -> str:
    """Generate a GPT evaluation prompt from structured criteria."""

    if evaluator_type == "editor":
        return _generate_editor_prompt(criteria, total_points)
    if criteria_type == "written":
        return _generate_written_prompt(criteria, total_points)
    return _generate_video_prompt(criteria, total_points)


def _generate_written_prompt(criteria: list[dict], total_points: int) -> str:
    """Generate prompt for evaluating written form answers."""
    criteria_text = ""
    json_fields = ""

    for i, c in enumerate(criteria, 1):
        criteria_text += f"\nCRITERIO {i} — {c['name']} (maximo {c['max_points']} puntos):\n"
        for level in c.get("levels", []):
            criteria_text += f"- {level['points']} puntos: {level['description']}\n"

        field_name = f"criterio_{i}"
        json_fields += f'"{field_name}_score": <0-{c["max_points"]}>, "{field_name}_name": "{c["name"]}", "{field_name}_reason": "<explanation>", '

    prompt = f"""You are an expert evaluator for a personnel selection process. Evaluate the candidate's written answers against the criteria below and return ONLY valid JSON, no markdown.

## CANDIDATE'S ANSWERS

{{answers}}

## EVALUATION CRITERIA ({total_points} points total)

{criteria_text}

## INSTRUCTIONS

- Evaluate the INTENT and quality of each answer, not exact wording
- Be fair but rigorous
- Provide specific reasons for each score

Return ONLY this JSON:
{{{json_fields}"puntuacion_total": <sum, max {total_points}>, "resumen": "Score: X/{total_points} — <2 sentences about overall performance>"}}"""

    return prompt


def _generate_editor_prompt(criteria: list[dict], total_points: int) -> str:
    """Generate prompt for evaluating multiple-choice editor questionnaires.

    Differences vs the sales/written prompt:
      - The model maps each answer literally to a predefined option, no intent eval.
      - DESCARTAR options force descalificado=true and total=0.
    """
    criteria_text = ""
    json_fields = ""
    has_disqualifying = False

    for i, c in enumerate(criteria, 1):
        criteria_text += f"\nCRITERIO {i} — {c['name']} (maximo {c['max_points']} puntos):\n"
        for level in c.get("levels", []):
            disq = " [DESCARTA]" if level.get("disqualifies") else ""
            criteria_text += f"- {level['points']} puntos{disq}: {level['description']}\n"
            if level.get("disqualifies"):
                has_disqualifying = True

        field_name = f"criterio_{i}"
        json_fields += f'"{field_name}_score": <0-{c["max_points"]}>, "{field_name}_name": "{c["name"]}", "{field_name}_reason": "<which option matched>", '

    disq_clause = ""
    if has_disqualifying:
        disq_clause = (
            "\n- Si la respuesta del candidato matchea una opción [DESCARTA], "
            "asigna 0 puntos a ese criterio Y devuelve `descalificado: true` "
            "Y `puntuacion_total: 0` en el JSON final, sin importar los demás criterios."
        )

    prompt = f"""Eres un evaluador de un cuestionario de selección. Cada criterio tiene opciones con puntaje fijo. Tu trabajo es MAPEAR la respuesta del candidato a la opción más cercana y asignar el puntaje predefinido. NO evalúes intención ni profundidad — esto es matching literal.

## RESPUESTAS DEL CANDIDATO

{{answers}}

## CRITERIOS DE EVALUACIÓN ({total_points} puntos máximo)

{criteria_text}

## INSTRUCCIONES

- Para cada criterio, identifica cuál de las opciones definidas describe mejor la respuesta del candidato y asigna ese puntaje.
- Si la respuesta no matchea ninguna opción claramente, asigna 0 y explícalo en el reason.
- En `*_reason`, di brevemente qué opción matcheó (no evalúes calidad).{disq_clause}

Devuelve ÚNICAMENTE este JSON, sin markdown:
{{{json_fields}"descalificado": <true|false>, "puntuacion_total": <sum, max {total_points}>, "resumen": "Score: X/{total_points} — <1-2 oraciones>"}}"""

    return prompt


def _generate_video_prompt(criteria: list[dict], total_points: int) -> str:
    """Generate prompt for evaluating video roleplay."""
    criteria_text = ""
    json_fields = ""

    for i, c in enumerate(criteria, 1):
        criteria_text += f"\nCRITERIO {i} — {c['name']} (maximo {c['max_points']} puntos):\n"
        if c.get("description"):
            criteria_text += f"{c['description']}\n"
        for level in c.get("levels", []):
            criteria_text += f"- {level['points']} puntos: {level['description']}\n"

        field_name = f"criterio_{i}"
        json_fields += f'"{field_name}_score": <0-{c["max_points"]}>, "{field_name}_name": "{c["name"]}", "{field_name}_reason": "<explanation>", '

    prompt = f"""You are an expert evaluator for sales roleplay calls. Evaluate the transcription against the criteria below and return ONLY valid JSON, no markdown.

## CANDIDATE DATA

Duration: {{duracion_segundos}} seconds

## TRANSCRIPTION

{{text}}

## FLUENCY DATA (AI-analyzed)

Pauses detected: {{pausas}}
Pause details: {{pausas_detalle}}
Total filler words: {{muletillas_conteo}}
Breakdown: ehm={{muletillas_ehm}}, este={{muletillas_este}}, "o sea"={{muletillas_osea}}, bueno={{muletillas_bueno}}, como={{muletillas_como}}, entonces={{muletillas_entonces}}
Filler details: {{muletillas_detalle}}
Continuity score (1-5): {{continuidad_puntaje}}
Continuity details: {{continuidad_detalle}}

## EVALUATION CRITERIA ({total_points} points total)

{criteria_text}

## INSTRUCTIONS

- Evaluate if the candidate covered the INTENT of each part, not exact wording
- Be fair but rigorous
- Provide specific reasons for each score

Return ONLY this JSON:
{{{json_fields}"puntuacion_total": <sum, max {total_points}>, "resumen": "Roleplay: X/{total_points} — <2 sentences about overall performance>"}}"""

    return prompt
