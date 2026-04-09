import json
import os
import re

from openai import OpenAI

from tools.logger import get_logger

log = get_logger("criteria_parser")

SYSTEM_PROMPT = """You are an expert at parsing evaluation criteria for personnel selection processes.
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


def parse_criteria(raw_text: str, criteria_type: str) -> dict:
    """Parse raw criteria text into structured JSON using GPT-4o."""
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    context = "written form answers" if criteria_type == "written" else "video roleplay performance"
    user_msg = f"Parse the following evaluation criteria for {context}:\n\n---\n{raw_text}\n---"

    log.info(f"Parsing {criteria_type} criteria with GPT-4o...")
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
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


def generate_evaluation_prompt(criteria: list[dict], total_points: int, criteria_type: str) -> str:
    """Generate a GPT evaluation prompt from structured criteria."""

    if criteria_type == "written":
        return _generate_written_prompt(criteria, total_points)
    else:
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
