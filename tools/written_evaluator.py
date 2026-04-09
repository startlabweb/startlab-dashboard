import json
import os
import re

from openai import OpenAI

from tools.logger import get_logger

log = get_logger("written_evaluator")


def evaluate_written_answers(
    answers: dict[str, str],
    prompt_template: str,
    candidate_info: dict,
) -> dict:
    """Evaluate written form answers with GPT-4o.

    Args:
        answers: Dict of {question: answer} from the form
        prompt_template: The GPT prompt with criteria (from criteria table)
        candidate_info: Dict with row_number, name, etc.
    """
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    # Build the answers section
    answers_text = ""
    for question, answer in answers.items():
        answers_text += f"**Q: {question}**\nA: {answer}\n\n"

    # Replace placeholder in template
    prompt = prompt_template.replace("{answers}", answers_text)
    prompt = prompt.replace("{nombre}", candidate_info.get("name", "Unknown"))

    row = candidate_info.get("row_number", "?")
    log.info(f"Evaluating written answers for row {row} with GPT-4o...")

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
            log.error(f"GPT did not return valid JSON for written eval: {e}")
            return {"error": str(e), "raw": raw}

    total = data.get("puntuacion_total", -1)
    log.info(f"Row {row} written: {total} pts — {data.get('resumen', '')[:80]}")
    return data
