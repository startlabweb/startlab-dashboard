"""Manda el correo con el formulario a quienes Paula deja en la planilla.

El flujo, entero:

    Paula escribe nombre y mail en la planilla de asignados
        -> el sistema ve la fila sin fecha de envio
        -> le manda el correo con el link del formulario
        -> escribe la fecha en la columna `Fecha de envio`

Paula NO llena esa columna: la escribe el sistema. Es el registro de que el
correo salio, y es lo unico que evita que a la misma persona le llegue dos veces.
Va en hora de Chile porque la va a leer el equipo, no una maquina. Por eso la planilla se comparte
con la cuenta de servicio como EDITOR y no como lector: sin poder escribir esa
celda, cada ciclo volveria a mandar el mismo correo.

Se escribe la fecha DESPUES de que Gmail confirma. Si el proceso muere en el
medio, el peor caso es un correo repetido; al reves seria un candidato que nunca
recibe nada y nadie se enteraria.
"""

from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo

    _TZ = ZoneInfo("America/Santiago")
except Exception:  # sin tzdata en la imagen: mejor UTC que reventar
    _TZ = timezone.utc

from app.config import settings
from tools import correo
from tools.logger import get_logger
from tools.sheet_reader import get_worksheet

log = get_logger("invitaciones")

# Los tres encabezados que la planilla tiene que tener, escritos asi. El sistema
# no los crea: si falta uno, avisa y no manda nada. Crear columnas por codigo es
# como se llega a escribir en la columna equivocada sin que nadie lo note.
COL_NOMBRE = "Nombre"
COL_EMAIL = "Email"
COL_ENVIADO = "Fecha de envío"


def _abrir_hoja(hoja_id: str, tab: str | None):
    """La hoja indicada, o la primera si no se indico ninguna.

    Adivinar el nombre de la primera hoja ("Hoja 1" o "Sheet1" segun el idioma
    de quien la creo) es una fuente de errores boba, asi que se permite no
    decirlo.
    """
    if (tab or "").strip():
        return get_worksheet(hoja_id, tab)
    from tools import sheets_limiter as limiter
    from tools.sheet_reader import get_gspread_client

    limiter.acquire(cost=2)
    return get_gspread_client().open_by_key(hoja_id).get_worksheet(0)


def _indice(headers: list[str], nombre: str) -> int | None:
    objetivo = nombre.strip().lower()
    for i, h in enumerate(headers):
        if (h or "").strip().lower() == objetivo:
            return i
    return None


def _valido(email: str) -> bool:
    e = (email or "").strip()
    return "@" in e and "." in e.split("@")[-1] and " " not in e


def invitar_pendientes(limite: int = 25) -> dict:
    """Manda el formulario a quien falte. Devuelve el resumen de lo que hizo.

    `limite` es un freno de mano: si alguien pega 300 filas de una, la primera
    corrida manda 25 y se ve el resultado antes de seguir.
    """
    hoja_id = (settings.IQ_HOJA_ASIGNADOS or "").strip()
    if not hoja_id:
        return {"error": "Falta IQ_HOJA_ASIGNADOS (la planilla donde asigna Paula)"}
    if not (settings.IQ_LINK_FORM or "").strip():
        return {"error": "Falta IQ_LINK_FORM (el link del formulario)"}

    # Sin nombre de hoja se usa la primera. Adivinar el nombre ("Hoja 1" o
    # "Sheet1" segun el idioma de quien la creo) es una fuente de errores boba.
    ws = _abrir_hoja(hoja_id, settings.IQ_HOJA_ASIGNADOS_TAB)
    filas = ws.get_all_values()
    if not filas:
        return {"error": "La planilla esta vacia"}

    headers = filas[0]
    i_nombre, i_email, i_enviado = (
        _indice(headers, COL_NOMBRE),
        _indice(headers, COL_EMAIL),
        _indice(headers, COL_ENVIADO),
    )
    faltan = [
        n
        for n, i in ((COL_NOMBRE, i_nombre), (COL_EMAIL, i_email), (COL_ENVIADO, i_enviado))
        if i is None
    ]
    if faltan:
        return {
            "error": (
                f"A la planilla le faltan las columnas {faltan}. Se crean a mano, "
                "con ese nombre exacto, antes de mandar nada."
            )
        }

    resumen = {"enviados": 0, "salteados": 0, "errores": [], "simulados": 0}
    marcas: list[tuple[int, str]] = []

    for n, fila in enumerate(filas[1:], start=2):
        if len(resumen["errores"]) + resumen["enviados"] + resumen["simulados"] >= limite:
            break

        def celda(i):
            return (fila[i] if i < len(fila) else "").strip()

        email, nombre, enviado = celda(i_email), celda(i_nombre), celda(i_enviado)

        if not email and not nombre:
            continue                       # fila vacia
        if enviado:
            resumen["salteados"] += 1      # ya se le mando
            continue
        if not _valido(email):
            resumen["errores"].append(f"fila {n}: mail invalido o vacio")
            continue

        asunto, cuerpo = correo.cargar_plantilla(
            "correo_form.md",
            nombre=nombre or "",
            link_form=settings.IQ_LINK_FORM,
        )
        r = correo.enviar(email, asunto, cuerpo)

        if r["enviado"]:
            resumen["enviados"] += 1
            marcas.append((n, datetime.now(_TZ).strftime("%Y-%m-%d %H:%M")))
        elif "simulacion" in r["motivo"]:
            resumen["simulados"] += 1
        else:
            resumen["errores"].append(f"fila {n}: {r['motivo']}")

    # Las marcas van al final y de una: una escritura por fila serian 25 llamadas
    # a la API de Sheets, que tiene cuota por minuto.
    for n, cuando in marcas:
        try:
            ws.update_cell(n, i_enviado + 1, cuando)
        except Exception as e:
            # El correo YA salio. Que no se pueda marcar es grave porque el
            # proximo ciclo lo repetiria, asi que se grita.
            log.error(f"Correo enviado en la fila {n} pero no se pudo marcar: {e}")
            resumen["errores"].append(
                f"fila {n}: el correo salio pero no se pudo marcar como enviado"
            )

    log.info(f"Invitaciones: {resumen}")
    return resumen
