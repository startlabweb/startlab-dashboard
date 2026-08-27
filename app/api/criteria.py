import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import database as db
from app.services.criteria_parser import parse_criteria, generate_evaluation_prompt

router = APIRouter()


class CriteriaUploadRequest(BaseModel):
    raw_text: str
    criteria_type: str  # 'written' or 'video'
    # Si viene, se guarda TAL CUAL y se saltea el parser de IA.
    #
    # Por que existe: el parser tiene como regla explicita "si no se especifican
    # los puntajes, inferí unos razonables" y "si no hay umbrales, creá cortes
    # sensatos". Para una rubrica donde los puntos son exactos (2 por pregunta,
    # 2+1+3 en el desarrollo) eso es inaceptable con 300 candidatos: la IA
    # inventaria umbrales distintos a los del documento.
    prompt_template: str | None = None
    total_points: int | None = None
    # Solo junto con prompt_template: el desglose de criterios (name + max_points)
    # para que la explicacion en el Sheet etiquete cada criterio por su nombre en
    # vez de una sola entrada "Rubrica fija". No pasa por el parser de IA.
    parsed_criteria: list[dict] | None = None


class CriteriaConfirmRequest(BaseModel):
    parsed_criteria: list[dict] | None = None  # optional edits


@router.post("/{monitor_id}/criteria")
async def upload_criteria(monitor_id: str, req: CriteriaUploadRequest):
    monitor = db.get_monitor(monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")

    if req.criteria_type not in ("written", "video", "iq"):
        raise HTTPException(
            status_code=400,
            detail="criteria_type must be 'written', 'video' or 'iq'",
        )

    evaluator_type = monitor.get("evaluator_type", "sales")
    if evaluator_type == "editor" and req.criteria_type in ("video", "iq"):
        raise HTTPException(
            status_code=400,
            detail="Editor evaluators do not support video or IQ criteria",
        )

    # La rubrica del IQ NO pasa por el parser de IA, nunca. Son dos casos con una
    # palanca correcta cada uno y una regla exacta de cuando se acierta: el parser
    # tiene como norma explicita inventar umbrales razonables cuando no los
    # encuentra, y eso aca cambiaria quien pasa el corte.
    if req.criteria_type == "iq" and not req.prompt_template:
        raise HTTPException(
            status_code=400,
            detail=(
                "La rubrica de IQ se carga textual: manda `prompt_template` con "
                "el contenido de prompts/consultor_iq.md, `total_points` y "
                "`parsed_criteria`. El parser de IA inventaria los umbrales."
            ),
        )

    if req.prompt_template:
        # Prompt fijo: no pasa por el parser, no se le inventa nada.
        if req.parsed_criteria:
            suma = sum(c.get("max_points", 0) for c in req.parsed_criteria)
            if req.total_points and suma != req.total_points:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"parsed_criteria suma {suma} pts pero total_points "
                        f"es {req.total_points}"
                    ),
                )
            criterios = req.parsed_criteria
        else:
            criterios = [
                {
                    "name": f"Rubrica fija ({req.criteria_type})",
                    "description": "Prompt cargado textual, sin parseo de IA",
                    "max_points": req.total_points or 0,
                }
            ]
        parsed = {
            "criteria": criterios,
            "total_points": req.total_points or 0,
            "notes": "Prompt fijo: los puntajes son los del documento, sin inferencia.",
        }
        prompt_template = req.prompt_template
    else:
        # Parse with AI
        parsed = await asyncio.to_thread(
            parse_criteria, req.raw_text, req.criteria_type, evaluator_type
        )

        if "error" in parsed:
            raise HTTPException(status_code=400, detail=parsed["error"])

        # Generate GPT prompt template
        prompt_template = generate_evaluation_prompt(
            parsed["criteria"], parsed["total_points"], req.criteria_type, evaluator_type
        )

    # Save to DB
    criteria_data = {
        "monitor_id": monitor_id,
        "criteria_type": req.criteria_type,
        "raw_text": req.raw_text,
        "parsed_criteria": parsed["criteria"],
        "total_points": parsed["total_points"],
        "gpt_prompt_template": prompt_template,
        "confirmed": False,
    }

    # Check if criteria already exists for this type
    existing = db.get_criteria_for_monitor(monitor_id, req.criteria_type)
    if existing:
        criteria = db.update_criteria(existing["id"], criteria_data)
    else:
        criteria = db.create_criteria(criteria_data)

    return {
        "id": criteria.get("id"),
        "criteria_type": req.criteria_type,
        "parsed_criteria": parsed["criteria"],
        "total_points": parsed["total_points"],
        "notes": parsed.get("notes", ""),
        "confirmed": False,
    }


@router.get("/{monitor_id}/criteria/{criteria_type}")
async def get_criteria(monitor_id: str, criteria_type: str):
    criteria = db.get_criteria_for_monitor(monitor_id, criteria_type)
    if not criteria:
        raise HTTPException(status_code=404, detail="Criteria not found")
    return criteria


@router.post("/{monitor_id}/criteria/{criteria_type}/confirm")
async def confirm_criteria(monitor_id: str, criteria_type: str, req: CriteriaConfirmRequest):
    monitor = db.get_monitor(monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")

    criteria = db.get_criteria_for_monitor(monitor_id, criteria_type)
    if not criteria:
        raise HTTPException(status_code=404, detail="Criteria not found")

    update_data = {"confirmed": True}

    # Mandar `parsed_criteria` en el confirm regenera el prompt con el parser
    # generico y pisa el prompt fijo que se acaba de cargar. Para el IQ eso es
    # directamente un error, no una advertencia en el runbook.
    if criteria_type == "iq" and req.parsed_criteria:
        raise HTTPException(
            status_code=400,
            detail=(
                "El confirm del IQ va con body {}: mandar parsed_criteria "
                "regeneraria el prompt con el parser y pisaria la rubrica fija."
            ),
        )

    # If user edited the criteria, regenerate the prompt
    if req.parsed_criteria:
        evaluator_type = monitor.get("evaluator_type", "sales")
        total = sum(c.get("max_points", 0) for c in req.parsed_criteria)
        prompt_template = generate_evaluation_prompt(
            req.parsed_criteria, total, criteria_type, evaluator_type
        )
        update_data.update({
            "parsed_criteria": req.parsed_criteria,
            "total_points": total,
            "gpt_prompt_template": prompt_template,
        })

    updated = db.update_criteria(criteria["id"], update_data)
    db.log_activity(monitor_id, "criteria_confirmed", f"Criteria ({criteria_type}) confirmed: {criteria.get('total_points')} pts")
    return updated
