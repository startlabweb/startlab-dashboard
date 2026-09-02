"""Envio de correos a candidatos. Apagado por defecto, a proposito.

Como manda: por la API de Gmail, con la misma cuenta de servicio que ya lee
Drive y escribe las planillas, haciendose pasar por un buzon real de Startlab.
Por eso no hace falta ningun proveedor nuevo, ninguna API key mas, ni verificar
un dominio: el correo sale de `@startlabweb.com` y no cae en spam.

Lo que SI hace falta, una vez: que un administrador de Google Workspace autorice
a esta cuenta de servicio el scope `gmail.send` (delegacion en todo el dominio) y
que `IQ_CORREO_REMITENTE` diga desde que buzon manda. Sin eso, `enviar` devuelve
un motivo claro en vez de fallar raro.

**Arranca en modo simulacion.** `IQ_CORREO_ACTIVO` tiene que ponerse en `true` a
mano para que salga un correo de verdad. Es la unica cosa de todo el sistema que
le escribe a una persona de afuera, y un bug aca no se puede deshacer: no existe
el "des-enviar". En simulacion se registra exactamente lo que se habria mandado.
"""

import base64
import json
import os
from email.message import EmailMessage
from pathlib import Path

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from app.config import settings
from tools.logger import get_logger

log = get_logger("correo")

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
PLANTILLAS = Path(__file__).resolve().parent.parent / "plantillas"


class ErrorCorreo(Exception):
    pass


def _servicio():
    remitente = (settings.IQ_CORREO_REMITENTE or "").strip()
    if not remitente:
        raise ErrorCorreo(
            "Falta IQ_CORREO_REMITENTE: hay que decir desde que buzon de "
            "Startlab manda el sistema."
        )
    info = json.loads(settings.GOOGLE_SERVICE_ACCOUNT_JSON)
    try:
        creds = Credentials.from_service_account_info(info, scopes=SCOPES).with_subject(
            remitente
        )
    except Exception as e:
        raise ErrorCorreo(f"No se pudo preparar la identidad del remitente: {e}")
    return build("gmail", "v1", credentials=creds), remitente


def cargar_plantilla(nombre: str, **valores) -> tuple[str, str]:
    """Lee una plantilla y devuelve (asunto, cuerpo).

    La primera linea es `ASUNTO: ...`. El resto es el cuerpo. Se deja como
    archivo y no en el codigo para que el texto lo pueda corregir Paula sin
    tocar Python -- es su correo, no nuestro.
    """
    ruta = PLANTILLAS / nombre
    if not ruta.exists():
        raise ErrorCorreo(f"No existe la plantilla {nombre}")
    texto = ruta.read_text(encoding="utf-8")

    lineas = texto.splitlines()
    if not lineas or not lineas[0].startswith("ASUNTO:"):
        raise ErrorCorreo(f"{nombre} tiene que empezar con 'ASUNTO: ...'")
    asunto = lineas[0][len("ASUNTO:"):].strip()
    cuerpo = "\n".join(lineas[1:]).strip()

    faltan = [f"{{{k}}}" for k in valores if f"{{{k}}}" not in texto]
    for k, v in valores.items():
        asunto = asunto.replace(f"{{{k}}}", str(v))
        cuerpo = cuerpo.replace(f"{{{k}}}", str(v))

    # Un placeholder sin reemplazar llegaria al candidato como "{link_form}".
    if "{" in cuerpo and "}" in cuerpo:
        sobrantes = [t for t in ("{nombre}", "{link}", "{link_form}") if t in cuerpo]
        if sobrantes:
            raise ErrorCorreo(
                f"La plantilla {nombre} quedo con {sobrantes} sin reemplazar"
            )
    return asunto, cuerpo


def enviar(para: str, asunto: str, cuerpo: str) -> dict:
    """Manda un correo. En simulacion no manda nada y lo dice.

    Devuelve {"enviado": bool, "motivo": str} -- nunca lanza por estar apagado,
    porque quien llama tiene que poder seguir con el resto de los candidatos.
    """
    if not settings.IQ_CORREO_ACTIVO:
        log.info(
            f"[SIMULACION] no se manda nada. Para: {para} | Asunto: {asunto} | "
            f"{len(cuerpo)} caracteres de cuerpo"
        )
        return {"enviado": False, "motivo": "simulacion (IQ_CORREO_ACTIVO apagado)"}

    try:
        servicio, remitente = _servicio()
    except ErrorCorreo as e:
        log.error(f"Correo sin configurar: {e}")
        return {"enviado": False, "motivo": str(e)}

    msg = EmailMessage()
    msg["To"] = para
    msg["From"] = remitente
    msg["Subject"] = asunto
    msg.set_content(cuerpo)

    crudo = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    try:
        r = servicio.users().messages().send(userId="me", body={"raw": crudo}).execute()
    except Exception as e:
        detalle = str(e)[:300]
        log.error(f"Gmail rechazo el envio a {para}: {detalle}")
        return {"enviado": False, "motivo": f"Gmail: {detalle}"}

    log.info(f"Correo enviado a {para} (id {r.get('id')})")
    return {"enviado": True, "motivo": "", "id": r.get("id")}
