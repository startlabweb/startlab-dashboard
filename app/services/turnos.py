"""Turnos de la sesion de IQ: el candidato elige horario y nunca hay dos a la vez.

Por que existen los turnos, cuando el diseño original era "entra cuando quiera":
**una licencia de Zoom no puede tener dos reuniones activas al mismo tiempo.**
Con dos candidatos simultaneos el segundo no entra, y la documentacion de Zoom
dice que iniciar una segunda reunion con "entrar antes que el anfitrion" puede
TERMINAR la primera sin aviso. O sea que un candidato podia cortarle el examen a
otro en la mitad. Se descubrio probando, no leyendo.

La garantia de que no haya dos a la vez no vive aca sino en la base, con un
indice unico sobre (monitor_id, iq_slot_at). Dos personas que aprietan el mismo
horario en el mismo segundo pasan cualquier validacion que se escriba en Python;
lo unico que detiene al segundo es que la base rechace su escritura.
"""

from datetime import datetime, timedelta, timezone

from app import database as db
from tools.logger import get_logger

log = get_logger("turnos")

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("America/Santiago")
except Exception:  # sin tzdata en la imagen
    TZ = timezone.utc

# Horario laborable, hora de Chile. La sesion dura 15 minutos y el turno reserva
# 30: el margen es para que la sala anterior termine de cerrarse antes de que la
# siguiente arranque. Sin ese margen el bot de la segunda sesion se encuentra la
# reunion de la primera todavia viva y queda golpeando la puerta.
HORA_DESDE = 9
HORA_HASTA = 18          # ultimo turno arranca 17:30
PASO_MINUTOS = 30
DIAS_HABILES = 10        # hasta donde se puede agendar
ANTICIPACION_MIN = 60    # no se puede agendar para dentro de menos de una hora


def _ocupados(monitor_id: str) -> set[str]:
    """Los horarios ya tomados, en ISO UTC."""
    r = (
        db.get_db()
        .table("candidates")
        .select("iq_slot_at")
        .eq("monitor_id", monitor_id)
        .not_.is_("iq_slot_at", "null")
        .execute()
    )
    return {x["iq_slot_at"][:16] for x in (r.data or []) if x.get("iq_slot_at")}


def disponibles(monitor_id: str) -> list[dict]:
    """Los turnos libres, agrupados por dia y ya en hora de Chile."""
    ocupados = _ocupados(monitor_id)
    ahora = datetime.now(TZ)
    minimo = ahora + timedelta(minutes=ANTICIPACION_MIN)

    dias: list[dict] = []
    dia = ahora.date()
    habiles = 0
    while habiles < DIAS_HABILES:
        if dia.weekday() < 5:  # 0-4 = lunes a viernes
            habiles += 1
            libres = []
            for hora in range(HORA_DESDE, HORA_HASTA):
                for minuto in range(0, 60, PASO_MINUTOS):
                    t = datetime(dia.year, dia.month, dia.day, hora, minuto, tzinfo=TZ)
                    if t < minimo:
                        continue
                    utc = t.astimezone(timezone.utc)
                    if utc.strftime("%Y-%m-%dT%H:%M") in ocupados:
                        continue
                    libres.append(
                        {
                            "iso": utc.isoformat(),
                            "hora": t.strftime("%H:%M"),
                        }
                    )
            if libres:
                dias.append(
                    {
                        "fecha": dia.isoformat(),
                        "etiqueta": _etiqueta_dia(dia),
                        "turnos": libres,
                    }
                )
        dia += timedelta(days=1)
    return dias


_DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
          "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def _etiqueta_dia(d) -> str:
    return f"{_DIAS[d.weekday()]} {d.day} de {_MESES[d.month - 1]}"


def reservar(candidate_id: str, monitor_id: str, iso_utc: str) -> dict:
    """Toma un turno. Devuelve {'ok': bool, 'motivo': str}.

    El choque se detecta por el error de la base y no comprobando antes: entre la
    comprobacion y la escritura hay milisegundos, y en esos milisegundos entra el
    otro candidato.
    """
    try:
        t = datetime.fromisoformat(iso_utc)
    except ValueError:
        return {"ok": False, "motivo": "Horario invalido"}
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)

    local = t.astimezone(TZ)
    if local.weekday() >= 5 or not (HORA_DESDE <= local.hour < HORA_HASTA):
        return {"ok": False, "motivo": "Ese horario esta fuera del horario laboral"}
    if t < datetime.now(timezone.utc) + timedelta(minutes=ANTICIPACION_MIN - 5):
        return {"ok": False, "motivo": "Ese horario ya paso o es demasiado pronto"}

    try:
        db.update_candidate(candidate_id, {"iq_slot_at": t.isoformat()})
    except Exception as e:
        detalle = str(e)
        if "duplicate" in detalle.lower() or "unique" in detalle.lower():
            log.info(f"Turno {iso_utc} ya tomado, choque en la base")
            return {"ok": False, "motivo": "Alguien acaba de tomar ese horario"}
        log.error(f"No se pudo reservar el turno: {detalle[:200]}")
        return {"ok": False, "motivo": "No se pudo reservar. Intenta de nuevo."}

    log.info(f"Turno reservado: {local.strftime('%Y-%m-%d %H:%M')} (Chile)")
    return {"ok": True, "motivo": "", "hora_local": local.strftime("%H:%M"),
            "etiqueta": _etiqueta_dia(local.date())}


def en_ventana(iso_utc: str | None, antes_min: int = 5, despues_min: int = 20) -> bool:
    """Si el turno esta lo bastante cerca para dejar entrar al candidato.

    Se abre 5 minutos antes (para que no golpee la puerta si llega puntual) y se
    cierra 20 despues, que es cuando ya no alcanza para dos casos de 6 minutos.
    """
    if not iso_utc:
        return False
    t = datetime.fromisoformat(iso_utc)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    ahora = datetime.now(timezone.utc)
    return t - timedelta(minutes=antes_min) <= ahora <= t + timedelta(minutes=despues_min)
