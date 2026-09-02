"""Crea las reuniones del IQ Test por API, para que nadie las cree a mano.

Por que la reunion la crea el sistema y no una persona: el embudo no tiene
humano en el medio. El candidato agenda, y de ahi sale una sala propia para el.
Una sala fija reusada por todos tambien seria automatica, pero dos candidatos a
la misma hora chocarian en la misma reunion.

Las dos configuraciones que hacen que el bot entre solo -- sala de espera
apagada y entrar antes que el anfitrion -- se ponen ACA, en la llamada de
creacion. No hay casillas que tildar en ningun panel.

Usa una app Server-to-Server OAuth de la cuenta (ZOOM_ACCOUNT_ID / ZOOM_CLIENT_ID
/ ZOOM_CLIENT_SECRET). El token dura una hora y se pide de nuevo cuando vence.
"""

import base64
import json
import secrets
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.zoom.us/v2"
OAUTH = "https://zoom.us/oauth/token"

_token_cache: dict = {"valor": None, "vence": 0.0}


class ErrorZoom(Exception):
    pass


def _env(nombre: str) -> str:
    v = os.getenv(nombre)
    if not v:
        raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ruta = os.path.join(raiz, ".env")
        if os.path.exists(ruta):
            for linea in open(ruta, encoding="utf-8"):
                if "=" in linea and not linea.strip().startswith("#"):
                    k, val = linea.split("=", 1)
                    if k.strip() == nombre:
                        v = val.strip().strip('"').strip("'")
                        break
    if not v:
        raise ErrorZoom(f"Falta {nombre}")
    return v


def _pedir(metodo: str, ruta: str, cuerpo: dict | None = None) -> dict:
    req = urllib.request.Request(
        API + ruta,
        data=json.dumps(cuerpo).encode() if cuerpo is not None else None,
        method=metodo,
    )
    req.add_header("Authorization", f"Bearer {token()}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            texto = r.read().decode()
            return json.loads(texto) if texto.strip() else {}
    except urllib.error.HTTPError as e:
        raise ErrorZoom(f"Zoom respondio {e.code}: {e.read().decode()[:400]}")


def token() -> str:
    """Token de la cuenta. Se cachea: dura una hora y pedirlo de nuevo es gratis
    pero lento, y una sesion puede crear varias reuniones seguidas."""
    if _token_cache["valor"] and time.time() < _token_cache["vence"]:
        return _token_cache["valor"]

    basic = base64.b64encode(
        f"{_env('ZOOM_CLIENT_ID')}:{_env('ZOOM_CLIENT_SECRET')}".encode()
    ).decode()
    url = OAUTH + "?" + urllib.parse.urlencode(
        {"grant_type": "account_credentials", "account_id": _env("ZOOM_ACCOUNT_ID")}
    )
    req = urllib.request.Request(url, data=b"", method="POST")
    req.add_header("Authorization", f"Basic {basic}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        raise ErrorZoom(f"No se pudo autenticar con Zoom: {e.read().decode()[:300]}")

    _token_cache["valor"] = d["access_token"]
    # Un minuto de margen para no usar un token que vence en el medio.
    _token_cache["vence"] = time.time() + int(d.get("expires_in", 3600)) - 60
    return _token_cache["valor"]


def crear_reunion(
    titulo: str,
    cuando: datetime | None = None,
    duracion_min: int = 30,
) -> dict:
    """Crea la sala de una sesion y devuelve sus datos.

    `cuando` en UTC; sin eso arranca ya. La duracion es la declarada, no un
    limite: Zoom no corta la reunion al llegar.
    """
    inicio = (cuando or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cuerpo = {
        "topic": titulo,
        "type": 2,  # agendada. La instantanea no acepta start_time.
        "start_time": inicio.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration": duracion_min,
        "timezone": "UTC",
        # Codigo de acceso propio, aleatorio y distinto por sesion. No es un
        # adorno: la cuenta tiene bloqueada la exigencia de que toda reunion
        # tenga UNA opcion de seguridad (codigo, sala de espera o "solo usuarios
        # autenticados"). Si no ponemos codigo, Zoom elige por nosotros -- y las
        # otras dos dejan al bot afuera. Viaja dentro del link, asi que el
        # candidato no tiene que escribirlo.
        "password": f"{secrets.randbelow(10**8):08d}",
        "settings": {
            # Las que hacen que el bot entre sin que nadie lo admita.
            "waiting_room": False,
            "join_before_host": True,
            "jbh_time": 0,          # puede entrar desde el minuto cero
            "approval_type": 2,     # sin registro previo
            # Un bot es un invitado anonimo: con esto en True, Zoom lo manda a la
            # sala de espera aunque la sala de espera este apagada. Fue lo que
            # rompio la cuarta prueba, y desde afuera parecia un problema de
            # sala de espera.
            "meeting_authentication": False,
            # Grabacion en la nube de Zoom. La grabacion de Recall NO incluye la
            # voz del propio bot, asi que la unica forma de tener la sesion
            # completa -- las preguntas de la IA y las respuestas del candidato
            # en el mismo archivo -- es que la grabe Zoom.
            "auto_recording": "cloud",
            "mute_upon_entry": False,
            "host_video": False,
            "participant_video": False,
            "audio": "voip",
        },
    }
    m = _pedir("POST", "/users/me/meetings", cuerpo)

    # Si Zoom ignorara alguna de las dos, el bot se quedaria golpeando la puerta
    # y la sesion se perderia sin que nadie entienda por que. Mejor saberlo aca.
    s = m.get("settings", {})
    problemas = []
    if s.get("waiting_room"):
        problemas.append("sala de espera encendida")
    if not s.get("join_before_host"):
        problemas.append("no se puede entrar antes que el anfitrion")
    if s.get("meeting_authentication"):
        problemas.append("exige usuarios autenticados (el bot es anonimo)")
    if not m.get("password"):
        problemas.append("sin codigo de acceso")
    if problemas:
        raise ErrorZoom(
            f"La reunion {m.get('id')} quedo con: {', '.join(problemas)}. "
            "La politica de la cuenta esta pisando la configuracion y el bot no "
            "va a poder entrar solo."
        )
    return m


def borrar_reunion(meeting_id) -> None:
    _pedir("DELETE", f"/meetings/{meeting_id}")


if __name__ == "__main__":
    import sys

    titulo = sys.argv[1] if len(sys.argv) > 1 else "Business IQ Test — prueba"
    m = crear_reunion(titulo)
    print(f"id       : {m['id']}")
    print(f"tema     : {m['topic']}")
    print(f"join_url : {m['join_url']}")
    s = m.get("settings", {})
    print(f"sala de espera: {s.get('waiting_room')} | entrar antes: {s.get('join_before_host')}")
