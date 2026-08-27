"""Sesiones de Meet: listar la carpeta de grabaciones y matchearlas con candidatos.

La sesion del Business IQ Test se hace en Google Meet con grabacion y
transcripcion automaticas, asi que de cada sesion quedan hasta tres archivos en
la carpeta de Drive:

    Entrevista - Consultor de negocios (Nombre Apellido) - <fecha> - Transcript
    Entrevista - Consultor de negocios (Nombre Apellido) - <fecha> - Recording
    Entrevista - Consultor de negocios (Nombre Apellido) - <fecha> - Notes by Gemini

Se usa **la transcripcion** (un Doc: exportarla es 1 request y $0), y solo si no
esta se cae al **Recording** (bajar el audio + AssemblyAI, ~$0.20). Las notas de
Gemini se ignoran a proposito: son un resumen interpretado por otro modelo, no lo
que el candidato dijo, y la nota tiene que poder defenderse.

El matcheo sale del nombre entre parentesis del titulo. Si un archivo no matchea
con ningun candidato, o matchea con mas de uno, **no se adivina**: queda en el log
y en la actividad del monitor. Poner la nota de una sesion en la fila de otra
persona es el error mas caro que puede cometer este sistema.
"""

import re
import unicodedata

from googleapiclient.errors import HttpError

from tools.drive_metadata import get_drive_service
from tools.logger import get_logger

log = get_logger("meet_recordings")

SUFIJO_TRANSCRIPT = "- transcript"
SUFIJO_RECORDING = "- recording"
# Las notas de Gemini terminan asi; nunca se usan como fuente.
SUFIJO_NOTAS = "- notes by gemini"

_RE_PARENTESIS = re.compile(r"\(([^)]{2,120})\)")


def normalizar(texto: object) -> str:
    """Minusculas, sin acentos, sin puntuacion y con los espacios colapsados.

    "Alex Yoseff " y "alex  yoseff" tienen que ser la misma clave: los nombres
    llegan tipeados a mano en el Form, en el titulo del evento de Calendar y en
    la planilla, y las tres versiones difieren.
    """
    s = str(texto or "")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9n ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _tokens(nombre: str) -> set[str]:
    # Se descartan los tokens de 1 letra (iniciales): "Maria T. Valle" y
    # "Maria Valle" son la misma persona.
    return {t for t in normalizar(nombre).split() if len(t) > 1}


def _clasificar(nombre_archivo: str, mime: str) -> str | None:
    n = nombre_archivo.lower().strip()
    if n.endswith(SUFIJO_NOTAS):
        return None
    if n.endswith(SUFIJO_TRANSCRIPT) and mime == "application/vnd.google-apps.document":
        return "transcript"
    if n.endswith(SUFIJO_RECORDING) and mime.startswith("video/"):
        return "recording"
    return None


def listar_sesiones(folder_id: str, titulo_prefijo: str | None = None) -> list[dict]:
    """Archivos de sesion de la carpeta, con el nombre del candidato ya extraido.

    `titulo_prefijo` filtra por el titulo del evento ("Entrevista - Consultor de
    negocios"): en la misma carpeta conviven mentorias y llamadas de venta, y sin
    el prefijo cualquiera de esas podria matchear con un candidato homonimo.
    """
    service = get_drive_service()
    prefijo = normalizar(titulo_prefijo) if titulo_prefijo else ""

    salida: list[dict] = []
    token = None
    while True:
        try:
            respuesta = (
                service.files()
                .list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields="nextPageToken, files(id, name, mimeType, size, createdTime)",
                    pageSize=200,
                    pageToken=token,
                    orderBy="createdTime desc",
                    # La carpeta de grabaciones puede vivir en una unidad
                    # compartida: sin estos dos flags la Drive API devuelve cero
                    # archivos y el sintoma es "nunca matchea nada", sin error.
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
        except HttpError as e:
            log.error(
                f"No se pudo listar la carpeta {folder_id}: {e.resp.status} {e.reason}. "
                f"Revisar que este compartida con la cuenta de servicio."
            )
            return []

        for f in respuesta.get("files", []):
            nombre = f.get("name", "")
            kind = _clasificar(nombre, f.get("mimeType", ""))
            if kind is None:
                continue
            if prefijo and not normalizar(nombre).startswith(prefijo):
                continue
            m = _RE_PARENTESIS.search(nombre)
            if not m:
                log.warning(
                    f"Sesion sin nombre entre parentesis, no se puede matchear: {nombre}"
                )
                continue
            salida.append(
                {
                    "file_id": f["id"],
                    "name": nombre,
                    "kind": kind,
                    "candidato_nombre": m.group(1).strip(),
                    "created_time": f.get("createdTime", ""),
                    "size_bytes": int(f.get("size") or 0),
                }
            )

        token = respuesta.get("nextPageToken")
        if not token:
            break

    return salida


def matchear(sesiones: list[dict], candidatos: list[dict]) -> dict:
    """Cruza sesiones con candidatos por nombre. No adivina.

    Args:
        sesiones: salida de `listar_sesiones`.
        candidatos: dicts con al menos `id`, `name` y `sheet_row`.

    Returns:
        {"matches": [{"candidate": ..., "sesion": ...}], "problemas": [...]}

    La transcripcion tiene prioridad sobre la grabacion, y un candidato se
    matchea una sola vez: si tiene dos archivos (Transcript y Recording), gana
    el Transcript.
    """
    por_nombre: dict[str, list[dict]] = {}
    for c in candidatos:
        clave = normalizar(c.get("name"))
        if clave:
            por_nombre.setdefault(clave, []).append(c)

    # Transcript primero; dentro de cada tipo, la sesion mas reciente primero.
    orden = {"transcript": 0, "recording": 1}
    ordenadas = sorted(
        sesiones,
        key=lambda s: (orden.get(s["kind"], 9), _invertir(s.get("created_time", ""))),
    )

    matches: list[dict] = []
    problemas: list[dict] = []
    ya_matcheados: set[str] = set()

    for s in ordenadas:
        clave = normalizar(s["candidato_nombre"])
        posibles = list(por_nombre.get(clave, []))

        if not posibles:
            # Segunda pasada, por tokens: cubre "Alex Yoseff" contra "Alex Yoseff
            # Rodriguez" y los nombres tipeados distinto en el Form y en Calendar.
            tokens_sesion = _tokens(s["candidato_nombre"])
            if tokens_sesion:
                for clave_c, lista in por_nombre.items():
                    tokens_c = _tokens(clave_c)
                    if not tokens_c:
                        continue
                    if tokens_sesion <= tokens_c or tokens_c <= tokens_sesion:
                        posibles.extend(lista)

        # Un mismo candidato puede venir repetido si entro por las dos pasadas.
        unicos = {c["id"]: c for c in posibles if c["id"] not in ya_matcheados}

        if not unicos and any(c["id"] in ya_matcheados for c in posibles):
            # Es el otro archivo de la MISMA sesion (el Recording cuando ya se
            # tomo el Transcript). No es un problema: se ignora sin ruido.
            continue

        if len(unicos) == 1:
            candidato = next(iter(unicos.values()))
            ya_matcheados.add(candidato["id"])
            matches.append({"candidate": candidato, "sesion": s})
        elif len(unicos) > 1:
            problemas.append(
                {
                    "motivo": "ambiguo",
                    "sesion": s["name"],
                    "detalle": ", ".join(
                        f"{c.get('name')} (fila {c.get('sheet_row')})"
                        for c in unicos.values()
                    ),
                }
            )
        else:
            problemas.append(
                {
                    "motivo": "sin_match",
                    "sesion": s["name"],
                    "detalle": s["candidato_nombre"],
                }
            )

    return {"matches": matches, "problemas": problemas}


def _invertir(iso: str) -> str:
    """Clave de orden descendente para un timestamp ISO, sin reverse=True.

    Hace falta porque la tupla de orden mezcla un criterio ascendente (el tipo:
    transcript antes que recording) con uno descendente (la fecha: la sesion mas
    nueva primero), y `reverse=True` daria vuelta los dos.
    """
    return "".join(chr(0x10FFFF - ord(c)) if ord(c) < 0x10FFFF else c for c in iso)


def exportar_texto(file_id: str) -> str:
    """Baja la transcripcion de Meet (un Google Doc) como texto plano.

    `export_media` alcanza con el scope `drive.readonly` que la cuenta de
    servicio ya tiene: no hay que tocar permisos ni sumar la Docs API.
    """
    service = get_drive_service()
    contenido = service.files().export(fileId=file_id, mimeType="text/plain").execute()
    if isinstance(contenido, bytes):
        return contenido.decode("utf-8", errors="replace")
    return str(contenido)
