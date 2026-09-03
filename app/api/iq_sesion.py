"""El unico link del candidato: elige horario, y a la hora entra a su sesion.

**Por que hay turnos.** La primera version no los tenia: el candidato entraba
cuando quisiera y la sala se creaba en ese momento. Se cayo al probarlo -- una
licencia de Zoom no puede tener dos reuniones activas a la vez, asi que con dos
candidatos simultaneos el segundo no entra, y la documentacion de Zoom dice que
iniciar una segunda reunion con "entrar antes que el anfitrion" puede TERMINAR la
primera sin aviso. Un candidato podia cortarle el examen a otro.

**Por que la sala se crea a la hora del turno y no al reservarlo.** Una sala
creada temprano es un bot cobrando por esperar, y ademas ocupa la unica reunion
activa que la licencia permite: el candidato de las 10 le bloquearia la sala al
de las 9:30 que todavia esta rindiendo.

Es un solo link a proposito, el mismo en el correo de principio a fin: sin
horario muestra los turnos, con horario dice cuando es, y en hora entra. Y es
idempotente -- recargar la pagina o volver al correo cae en la MISMA reunion, no
crea otra ni lanza un segundo bot.
"""

import secrets
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import database as db
from app.config import settings
from app.services import turnos
from tools import calendario, recall, zoom
from tools.logger import get_logger

router = APIRouter()
log = get_logger("iq_sesion")
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


class ReservaRequest(BaseModel):
    iso: str          # el horario elegido, en UTC


def _pagina(
    request: Request,
    titulo: str,
    texto: str,
    accion: str | None = None,
    token: str | None = None,
) -> HTMLResponse:
    """Una pantalla simple para el candidato. Sin dashboard ni datos internos.

    `accion` pone un boton que libera el turno y lo devuelve a elegir otro.
    """
    return templates.TemplateResponse(
        request=request, name="iq_mensaje.html",
        context={"titulo": titulo, "texto": texto, "accion": accion, "token": token},
    )


def _cuando_en_texto(cuando: datetime) -> str:
    """La fecha del turno en palabras.

    Va en hora de Chile porque esto se arma del lado del servidor y no sabemos
    donde esta la persona. La pantalla donde ELIGE si lo muestra en su zona (lo
    hace el navegador), y la invitacion de calendario tambien. Aca se aclara cual
    es para que nadie se confunda de huso.
    """
    local = cuando.astimezone(turnos.TZ)
    return (
        f"el {turnos._etiqueta_dia(local.date())} a las "
        f"{local.strftime('%H:%M')} (hora de Chile)"
    )


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
    try:
        r = (
            db.get_db()
            .table("candidates")
            .select("*")
            .eq("iq_session_token", token)
            .limit(1)
            .execute()
        )
    except Exception as e:
        # El caso concreto: la migracion 005 todavia no se corrio, asi que la
        # columna no existe. Sin esto el candidato ve un error de Postgres en
        # crudo, y el equipo tarda en entender que falta una migracion.
        if "iq_session_token" in str(e):
            log.error("Falta la migracion 005: la columna iq_session_token no existe")
            raise HTTPException(
                status_code=503,
                detail="El sistema de sesiones todavia no esta habilitado. "
                "Avisale al equipo que falta correr la migracion 005.",
            )
        raise

    if not r.data:
        raise HTTPException(status_code=404, detail="Link invalido o vencido")
    return r.data[0]


@router.post("/agendar/{token}")
async def agendar(token: str, req: ReservaRequest):
    """El candidato elige su horario."""
    c = _buscar_por_token(token)
    if (c.get("gate1_decision") or "pendiente") != "aprobado":
        raise HTTPException(status_code=403, detail="Link no habilitado")
    if c.get("iq_slot_at"):
        raise HTTPException(status_code=409, detail="Ya tienes un horario reservado")

    r = turnos.reservar(c["id"], c["monitor_id"], req.iso)
    if not r["ok"]:
        raise HTTPException(status_code=409, detail=r["motivo"])

    db.log_activity(
        c["monitor_id"], "iq_turno_reservado",
        f"{c.get('name')} agendo su sesion de IQ: {r['etiqueta']} a las {r['hora_local']}",
    )

    # La invitacion de calendario va DESPUES de reservar y no puede voltear la
    # reserva: el turno ya es suyo. Si Google falla, se queda sin recordatorio
    # pero con su horario y su link -- perder la invitacion no puede costarle la
    # sesion. Por eso el resultado solo se loguea.
    email = (c.get("email") or "").strip()
    if email:
        cuando = datetime.fromisoformat(req.iso)
        if cuando.tzinfo is None:
            cuando = cuando.replace(tzinfo=timezone.utc)
        inv = calendario.crear_evento(
            email=email,
            nombre=c.get("name") or "",
            cuando=cuando.astimezone(timezone.utc),
            link=f"{_base_publica()}/iq/entrar/{token}",
        )
        r["invitacion"] = inv["ok"]
        if not inv["ok"]:
            log.warning(
                f"{c.get('name')}: turno reservado pero sin invitacion de "
                f"calendario ({inv['motivo'][:120]})"
            )
    return r


@router.post("/reagendar/{token}")
async def reagendar(token: str):
    """Libera el turno del candidato para que elija otro.

    Existe porque a la gente le cambian los planes: sin esto, quien reservo un
    turno y no puede llegar no tiene forma de avisar, y el sistema le arma una
    sala a la que nadie entra. Liberar el turno tambien lo devuelve a la lista
    para otro candidato.

    No se puede reagendar una sesion ya empezada: si el bot existe, la sala esta
    creada y la persona ya rindio o esta rindiendo.
    """
    c = _buscar_por_token(token)
    if c.get("iq_bot_id") or c.get("iq_status") in ("completed", "processing", "pending"):
        raise HTTPException(
            status_code=409,
            detail="Tu sesión ya empezó o ya fue registrada, no se puede cambiar.",
        )

    db.update_candidate(c["id"], {"iq_slot_at": None})
    db.log_activity(
        c["monitor_id"], "iq_turno_liberado",
        f"{c.get('name')} libero su turno para elegir otro",
    )
    log.info(f"{c.get('name')}: turno liberado")
    return {"ok": True}


@router.get("/entrar/{token}", response_class=HTMLResponse)
async def entrar(request: Request, token: str):
    """El unico link del candidato. Hace una cosa distinta segun el momento:

    - sin horario elegido  -> le muestra los turnos libres
    - con horario, temprano -> le dice cuando es
    - con horario, en hora  -> le crea la sala y lo manda a Zoom

    Es un solo link a proposito: el que va en el correo no tiene que cambiar
    segun en que punto del proceso este la persona.
    """
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
        return _pagina(
            request, "Ya registramos tu sesión",
            "El equipo va a revisar tus diagnósticos y te va a escribir con el resultado.",
        )

    if (c.get("gate1_decision") or "pendiente") != "aprobado":
        raise HTTPException(status_code=403, detail="Link no habilitado")

    # Sin horario: elige uno.
    if not c.get("iq_slot_at"):
        return templates.TemplateResponse(
            request=request, name="iq_agenda.html",
            context={"token": token, "nombre": nombre.split(" ")[0],
                     "dias": turnos.disponibles(c["monitor_id"])},
        )

    # Con horario pero todavia lejos: no se crea nada. Crear la sala antes de
    # tiempo significa pagar un bot esperando, y encima ocuparia la unica reunion
    # que la licencia de Zoom permite tener activa.
    if not turnos.en_ventana(c["iq_slot_at"]):
        cuando = datetime.fromisoformat(c["iq_slot_at"])
        if cuando.tzinfo is None:
            cuando = cuando.replace(tzinfo=timezone.utc)

        # Turno ya pasado. Antes esta misma pantalla decia "te esperamos a las
        # 11:30" para una hora vencida, y el candidato no tenia como salir de
        # ahi: le quedaba un link que le prometia una sesion imposible.
        if cuando < datetime.now(timezone.utc):
            return _pagina(
                request, "Tu turno ya pasó",
                "No alcanzamos a tomarte la sesión en el horario que elegiste. "
                "No hay problema: elige otro y seguimos.",
                accion="Elegir otro horario", token=token,
            )

        return _pagina(
            request, "Tu sesión está agendada",
            f"Te esperamos {_cuando_en_texto(cuando)}. Vuelve a este mismo link "
            "cinco minutos antes y entrarás directo.",
            accion="Cambiar mi horario", token=token,
        )

    monitor = db.get_monitor(c["monitor_id"])
    if not monitor:
        raise HTTPException(status_code=500, detail="Monitor inexistente")

    # 1. La sala, con la sala de espera apagada por API para que el bot entre solo.
    #
    # Va envuelto porque lo que se cae aca no es culpa del candidato y no puede
    # verlo en crudo: la primera vez que esto fallo -- faltaban las credenciales
    # de Zoom en produccion -- la persona recibio un "Internal Server Error"
    # justo en el momento de rendir su examen.
    try:
        reunion = zoom.crear_reunion(
            titulo=f"Business IQ Test — {nombre}".strip(" —"),
            duracion_min=30,
        )
    except Exception as e:
        log.error(f"{nombre}: no se pudo crear la reunion de Zoom: {str(e)[:250]}")
        return _pagina(
            request, "No pudimos abrir tu sala",
            "Tuvimos un problema técnico de nuestro lado, no tuyo. Vuelve a "
            "cargar esta página en un minuto; si sigue igual, escríbenos al "
            "correo desde el que te invitamos y te damos otro horario.",
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
        bot = recall.crear_bot(
            reunion["join_url"],
            pagina,
            # Recall avisa aca cuando entra un participante, y recien
            # entonces la IA saluda. El token viaja en la URL para no
            # tener que cruzar el id del bot con nada.
            webhook_llegada=f"{_base_publica()}/api/iq/llego?s={quote(token)}",
        )
    except Exception as e:
        # Sin bot no hay sesion: se intenta borrar la reunion para no dejar salas
        # huerfanas, y se deja el link utilizable para reintentar.
        #
        # El borrado va en su propio try: si falla (por ejemplo porque a la app de
        # Zoom le falta el scope de borrado) taparia el error de verdad, y el
        # equipo terminaria buscando un problema de permisos de Zoom cuando lo que
        # se cayo fue Recall.
        try:
            zoom.borrar_reunion(reunion["id"])
        except Exception as e2:
            log.warning(f"No se pudo borrar la reunion {reunion['id']}: {e2}")
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
