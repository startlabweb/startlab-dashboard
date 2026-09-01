"""El link que el candidato abre desde el correo: crea su sesion y lo manda a Zoom.

Por que la sesion se arma recien cuando el candidato hace clic, y no antes:

- **No hay agendamiento.** El candidato entra cuando puede, no cuando le toca un
  turno. Se elimina Calendly, el calendario compartido y el poller que los mira.
- **No se paga un bot esperando a nadie.** Recall cobra por minuto de bot en la
  reunion; con turnos agendados se paga igual cuando el candidato no aparece.
- **Nunca hay dos candidatos en la misma sala**, que es lo que rompia la idea de
  una sala fija de Zoom reusada por todos.

Es idempotente a proposito: si el candidato recarga la pagina o vuelve a abrir el
correo, cae en la MISMA reunion en vez de crear otra y lanzar un segundo bot.
"""

import secrets
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

from app import database as db
from app.config import settings
from tools import recall, zoom
from tools.logger import get_logger

router = APIRouter()
log = get_logger("iq_sesion")


def nuevo_token() -> str:
    """Token del link del correo. Largo a proposito: es lo unico que protege la
    sesion de un candidato, y va en un correo que se puede reenviar."""
    return secrets.token_urlsafe(32)


def _base_publica() -> str:
    url = (settings.DASHBOARD_URL or "").strip().rstrip("/")
    if not url:
        raise HTTPException(
            status_code=500,
            detail="Falta DASHBOARD_URL: sin eso no se puede armar el link que abre el bot",
        )
    return url


def _buscar_por_token(token: str) -> dict:
    r = (
        db.get_db()
        .table("candidates")
        .select("*")
        .eq("iq_session_token", token)
        .limit(1)
        .execute()
    )
    if not r.data:
        raise HTTPException(status_code=404, detail="Link invalido o vencido")
    return r.data[0]


@router.get("/entrar/{token}")
async def entrar(token: str):
    """Crea la reunion y el bot si hacen falta, y manda al candidato a Zoom."""
    c = _buscar_por_token(token)
    nombre = c.get("name") or ""

    # Ya tiene sesion armada: se lo manda a la misma. Cubre el recargar la
    # pagina, el volver al correo, y el hacer doble clic en el link.
    if c.get("iq_session_url") and c.get("iq_bot_id"):
        log.info(f"{nombre}: vuelve a entrar a su sesion ya creada")
        return RedirectResponse(c["iq_session_url"], status_code=302)

    # Una sesion ya evaluada no se vuelve a abrir: seria pagar otra sesion y
    # pisar la nota que ya tiene.
    if c.get("iq_status") in ("completed", "processing", "pending"):
        raise HTTPException(
            status_code=409,
            detail="Tu sesion ya fue registrada. El equipo te va a escribir con el resultado.",
        )

    if (c.get("gate1_decision") or "pendiente") != "aprobado":
        raise HTTPException(status_code=403, detail="Link no habilitado")

    monitor = db.get_monitor(c["monitor_id"])
    if not monitor:
        raise HTTPException(status_code=500, detail="Monitor inexistente")

    # 1. La sala, con la sala de espera apagada por API para que el bot entre solo.
    reunion = zoom.crear_reunion(
        titulo=f"Business IQ Test — {nombre}".strip(" —"),
        duracion_min=30,
    )

    # 2. El bot, que abre la sala del IQ y la transmite a esa reunion. El nombre
    #    del candidato viaja en la URL para que la IA lo salude por su nombre;
    #    el guion y la voz se atan del lado del servidor al pedir la clave.
    pagina = (
        f"{_base_publica()}/iq/sala"
        f"?t={quote(settings.IQ_SALA_TOKEN)}&auto=1&limpio=1"
        f"&s={quote(token)}"
        f"&n={quote(nombre.split(' ')[0] if nombre else '')}"
    )
    try:
        bot = recall.crear_bot(reunion["join_url"], pagina)
    except Exception as e:
        # Sin bot no hay sesion: se borra la reunion para no dejar salas
        # huerfanas y se deja el link utilizable para reintentar.
        zoom.borrar_reunion(reunion["id"])
        log.error(f"{nombre}: no se pudo crear el bot, reunion descartada: {e}")
        raise HTTPException(
            status_code=502,
            detail="No pudimos abrir la sala. Intenta de nuevo en un minuto.",
        )

    db.update_candidate(
        c["id"],
        {
            "iq_meeting_id": str(reunion["id"]),
            "iq_session_url": reunion["join_url"],
            "iq_bot_id": bot.get("id"),
            "iq_bot_status": "creado",
            "iq_started_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    db.log_activity(
        monitor["id"],
        "iq_sesion_iniciada",
        f"{nombre} entro a su sesion de IQ (reunion {reunion['id']}, bot {bot.get('id')})",
    )
    log.info(f"{nombre}: sesion creada, reunion {reunion['id']}, bot {bot.get('id')}")

    return RedirectResponse(reunion["join_url"], status_code=302)
