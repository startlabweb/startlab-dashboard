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
import re
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


def cargar_plantilla(plantilla: str, **valores) -> tuple[str, str]:
    """Lee una plantilla y devuelve (asunto, cuerpo).

    El parametro se llama `plantilla` y no `nombre` porque las plantillas
    reciben justamente un `nombre` (el del candidato) entre sus valores: con el
    nombre viejo, `cargar_plantilla("correo_form.md", nombre="Jossy")` explotaba
    con "multiple values for argument". Reventaba en el primer envio real.

    La primera linea es `ASUNTO: ...`. El resto es el cuerpo. Se deja como
    archivo y no en el codigo para que el texto lo pueda corregir Paula sin
    tocar Python -- es su correo, no nuestro.
    """
    ruta = PLANTILLAS / plantilla
    if not ruta.exists():
        raise ErrorCorreo(f"No existe la plantilla {plantilla}")
    texto = ruta.read_text(encoding="utf-8")

    lineas = texto.splitlines()
    if not lineas or not lineas[0].startswith("ASUNTO:"):
        raise ErrorCorreo(f"{plantilla} tiene que empezar con 'ASUNTO: ...'")
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
                f"La plantilla {plantilla} quedo con {sobrantes} sin reemplazar"
            )
    return asunto, cuerpo


def _a_html(cuerpo: str) -> str:
    """Convierte el markdown de la plantilla a HTML.

    Por que existe: las plantillas se escriben en markdown para que Paula las
    pueda editar sin ver etiquetas, pero el correo se mandaba SOLO como texto
    plano -- asi que los `**` y los `###` le llegaban crudos al candidato. Es
    deliberadamente minimo: solo lo que las plantillas usan de verdad.
    """
    from html import escape

    salida: list[str] = []
    lista: list[str] = []

    def cerrar_lista():
        if lista:
            salida.append("<ol>" + "".join(f"<li>{x}</li>" for x in lista) + "</ol>")
            lista.clear()

    for linea in cuerpo.splitlines():
        cruda = linea.strip()
        if not cruda:
            cerrar_lista()
            continue

        seguro = escape(cruda)
        # Negritas y links, en ese orden: escapar primero para que un nombre con
        # `<` o `&` no rompa el HTML ni inyecte nada.
        seguro = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", seguro)
        seguro = re.sub(
            r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', seguro
        )

        if cruda.startswith("### "):
            cerrar_lista()
            salida.append(f"<h3>{seguro[4:]}</h3>")
        elif re.match(r"^\d+\.\s", cruda):
            lista.append(re.sub(r"^\d+\.\s*", "", seguro))
        else:
            cerrar_lista()
            salida.append(f"<p>{seguro}</p>")
    cerrar_lista()

    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;'
        'line-height:1.6;color:#222">' + "".join(salida) + "</div>"
    )


def _a_texto(cuerpo: str) -> str:
    """El cuerpo sin los simbolos del markdown, para la version de texto plano."""
    sin = re.sub(r"\*\*(.+?)\*\*", r"\1", cuerpo)
    return re.sub(r"^###\s*", "", sin, flags=re.MULTILINE)


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
    # Las dos versiones: texto plano sin los simbolos del markdown, y HTML con
    # el formato de verdad. El cliente de correo elige; si solo soporta texto,
    # ve el limpio y no los `**`.
    msg.set_content(_a_texto(cuerpo))
    msg.add_alternative(_a_html(cuerpo), subtype="html")

    crudo = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    try:
        r = servicio.users().messages().send(userId="me", body={"raw": crudo}).execute()
    except Exception as e:
        detalle = str(e)[:300]
        log.error(f"Gmail rechazo el envio a {para}: {detalle}")
        return {"enviado": False, "motivo": f"Gmail: {detalle}"}

    log.info(f"Correo enviado a {para} (id {r.get('id')})")
    return {"enviado": True, "motivo": "", "id": r.get("id")}
