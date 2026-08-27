"""Aviso a Slack por webhook entrante.

Por que un webhook y no la API de Slack: el unico mensaje que el sistema manda es
"estos candidatos pasaron el Gate 1, hay que aprobarlos a mano". No necesita leer
canales, ni hilos, ni identidad de usuario, asi que un webhook (una URL, cero
OAuth, cero tokens que rotar) es todo lo que hace falta.

Dos reglas de diseño:

1. **Nunca levanta excepcion.** Un aviso que falla no puede romper el ciclo del
   worker: se loguea y se devuelve False. El estado real vive en la base y en el
   Sheet; Slack es una notificacion, no un registro.
2. **Sin la variable configurada, el sistema funciona igual.** `SLACK_WEBHOOK_URL`
   vacio = no se avisa y se dice en el log. Asi el resto de los monitores (becas,
   editor, setter) no cambian en nada.
"""

import httpx

from app.config import settings
from tools.logger import get_logger

log = get_logger("slack")

TIMEOUT = 10.0


def configurado() -> bool:
    return bool(settings.SLACK_WEBHOOK_URL)


def enviar(texto: str) -> bool:
    """Manda un mensaje de texto (markdown de Slack). Devuelve si se pudo."""
    if not configurado():
        log.info("SLACK_WEBHOOK_URL sin configurar: no se manda el aviso")
        return False

    try:
        respuesta = httpx.post(
            settings.SLACK_WEBHOOK_URL,
            json={"text": texto, "unfurl_links": False, "unfurl_media": False},
            timeout=TIMEOUT,
        )
        if respuesta.status_code >= 300:
            log.error(
                f"Slack respondio {respuesta.status_code}: {respuesta.text[:200]}"
            )
            return False
        return True
    except Exception as e:
        log.error(f"No se pudo avisar a Slack: {e}")
        return False
