"""Aviso a Slack. Soporta dos vias: bot token o webhook entrante.

El unico mensaje que el sistema manda es "estos candidatos pasaron el Gate 1, hay
que aprobarlos a mano". No necesita leer canales, ni hilos, ni identidad de
usuario.

**Por que hay dos vias y no una.** El diseño original era solo webhook: una URL,
cero OAuth, cero tokens que rotar. Pero al ir a configurarlo no habia ningun
webhook de Start Lab y crear uno requiere autorizar una app dentro de Slack, que
es una accion de persona. En cambio ya existia un bot token del workspace con
`chat:write`, asi que se usa ese y se cierra el circuito hoy. El webhook sigue
soportado y tiene prioridad si algun dia se crea: no hay que rehacer nada.

Dos reglas de diseño que no cambian:

1. **Nunca levanta excepcion.** Un aviso que falla no puede romper el ciclo del
   worker: se loguea y se devuelve False. El estado real vive en la base y en el
   Sheet; Slack es una notificacion, no un registro.
2. **Sin configurar, el sistema funciona igual.** Sin webhook ni token no se
   avisa y se dice en el log. Asi los otros monitores (becas, editor, setter) no
   cambian en nada.
"""

import httpx

from app.config import settings
from tools.logger import get_logger

log = get_logger("slack")

TIMEOUT = 10.0
POST_MESSAGE = "https://slack.com/api/chat.postMessage"


def configurado() -> bool:
    return bool(settings.SLACK_WEBHOOK_URL) or bool(
        settings.SLACK_BOT_TOKEN and settings.SLACK_CANAL
    )


def _por_webhook(texto: str) -> bool:
    respuesta = httpx.post(
        settings.SLACK_WEBHOOK_URL,
        json={"text": texto, "unfurl_links": False, "unfurl_media": False},
        timeout=TIMEOUT,
    )
    if respuesta.status_code >= 300:
        log.error(f"Slack respondio {respuesta.status_code}: {respuesta.text[:200]}")
        return False
    return True


def _por_bot(texto: str) -> bool:
    respuesta = httpx.post(
        POST_MESSAGE,
        headers={"Authorization": f"Bearer {settings.SLACK_BOT_TOKEN}"},
        json={
            "channel": settings.SLACK_CANAL,
            "text": texto,
            "unfurl_links": False,
            "unfurl_media": False,
        },
        timeout=TIMEOUT,
    )
    datos = respuesta.json()
    if not datos.get("ok"):
        error = datos.get("error", "desconocido")
        # Los dos errores que de verdad pasan, con el arreglo escrito al lado:
        # sin ellos el log dice "not_in_channel" y nadie sabe que hacer.
        ayuda = {
            "not_in_channel": (
                "el bot no esta en el canal: hay que invitarlo con "
                "/invite @<bot> desde el canal"
            ),
            "channel_not_found": (
                "el canal no existe o el bot no lo ve: usar el ID del canal "
                "(Slack: detalles del canal, abajo) en vez del nombre"
            ),
        }.get(error, "")
        log.error(f"Slack rechazo el mensaje: {error}" + (f" -- {ayuda}" if ayuda else ""))
        return False
    return True


def enviar(texto: str) -> bool:
    """Manda un mensaje de texto (markdown de Slack). Devuelve si se pudo."""
    if not configurado():
        log.info("Slack sin configurar (ni webhook ni bot+canal): no se manda el aviso")
        return False

    try:
        # El webhook primero: si existe, es la via mas simple y la que menos
        # permisos arrastra.
        if settings.SLACK_WEBHOOK_URL:
            return _por_webhook(texto)
        return _por_bot(texto)
    except Exception as e:
        log.error(f"No se pudo avisar a Slack: {e}")
        return False
