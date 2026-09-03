"""Crea el evento de la sesion en Google Calendar e invita al candidato.

Para que existe: el candidato recibe una invitacion de verdad, con el recordatorio
de Google, y le queda en su calendario en SU zona horaria. Es lo unico que
Appointment Schedules daba y nuestro agendamiento no: sin esto, la persona elige
un turno y despues no tiene nada que se lo recuerde.

Usa la misma cuenta de servicio y la misma delegacion que el correo, haciendose
pasar por el buzon de `IQ_CORREO_REMITENTE`. Hace falta autorizarle un scope mas
(`calendar.events`) en el Admin de Workspace, al lado del de Gmail que ya esta.

**No levanta excepcion nunca.** El turno ya quedo reservado en la base cuando se
llama a esto: si Google falla, el candidato tiene su horario y su link igual, solo
se queda sin el recordatorio. Perder la invitacion no puede costarle la sesion.
"""

import json
from datetime import datetime, timedelta

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from app.config import settings
from tools.logger import get_logger

log = get_logger("calendario")

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
DURACION_MIN = 20   # 15 de sesion + margen, para que no se solape en su agenda


def configurado() -> bool:
    return bool(settings.IQ_CORREO_REMITENTE)


def crear_evento(
    email: str, nombre: str, cuando: datetime, link: str
) -> dict:
    """Invita al candidato a su sesion. Devuelve {'ok': bool, 'motivo': str}."""
    if not configurado():
        return {"ok": False, "motivo": "sin IQ_CORREO_REMITENTE"}

    remitente = settings.IQ_CORREO_REMITENTE.strip()
    try:
        info = json.loads(settings.GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES).with_subject(
            remitente
        )
        servicio = build("calendar", "v3", credentials=creds)
    except Exception as e:
        log.error(f"No se pudo preparar el calendario: {str(e)[:200]}")
        return {"ok": False, "motivo": str(e)[:200]}

    fin = cuando + timedelta(minutes=DURACION_MIN)
    evento = {
        "summary": "Business IQ Test — Start Lab",
        "description": (
            "Tu sesión del Business IQ Test de Start Lab.\n\n"
            f"Entra por este enlace a la hora acordada:\n{link}\n\n"
            "Ese mismo enlace te sirve para cambiar tu horario si lo necesitas.\n\n"
            "Son máximo 15 minutos. Te vamos a presentar dos casos de negocios "
            "online para que identifiques sus principales cuellos de botella.\n\n"
            "No hace falta que prepares nada. Esto no es una entrevista: la IA "
            "solo facilita el examen y recopila tus respuestas."
        ),
        # La hora va en UTC y Google la muestra a cada invitado en SU zona. Por
        # eso no se manda ninguna zona horaria nuestra: la conversion la hace su
        # calendario, que sabe donde esta la persona mejor que nosotros.
        "start": {"dateTime": cuando.isoformat(), "timeZone": "UTC"},
        "end": {"dateTime": fin.isoformat(), "timeZone": "UTC"},
        "attendees": [{"email": email, "displayName": nombre or email}],
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "email", "minutes": 60},
                {"method": "popup", "minutes": 10},
            ],
        },
        # Sin permiso para invitar a otros ni ver la lista: es una sesion de
        # examen, no una reunion de equipo.
        "guestsCanInviteOthers": False,
        "guestsCanSeeOtherGuests": False,
    }

    try:
        r = (
            servicio.events()
            .insert(calendarId="primary", body=evento, sendUpdates="all")
            .execute()
        )
    except Exception as e:
        detalle = str(e)[:300]
        if "insufficient" in detalle.lower() or "forbidden" in detalle.lower():
            log.error(
                "Calendar rechazo el evento: falta autorizarle el scope "
                "calendar.events a la cuenta de servicio en el Admin de Workspace"
            )
        else:
            log.error(f"Calendar rechazo el evento: {detalle}")
        return {"ok": False, "motivo": detalle}

    log.info(f"Invitacion de calendario enviada a {email} (evento {r.get('id')})")
    return {"ok": True, "motivo": "", "id": r.get("id"), "link": r.get("htmlLink")}
