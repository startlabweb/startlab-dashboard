"""La sala del Business IQ Test: la clave efimera y los datos de los casos.

Por que existe una clave efimera y no se manda la API key al navegador: la
pagina de la sala termina corriendo dentro de un Chrome de Recall que no
controlamos, y su URL es publica. `OPENAI_API_KEY` no puede bajar ahi. La clave
efimera dura minutos, ya viene atada al guion y a la voz, y no sirve para nada
mas que esa sesion.

Las instrucciones del agente se arman aca, del lado del servidor, por el mismo
motivo: si viajaran al navegador, el candidato podria leerlas.
"""

from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app import database as db

from app.config import settings
from tools.logger import get_logger

router = APIRouter()
log = get_logger("iq_agente")


class TranscripcionRequest(BaseModel):
    t: str | None = None      # token de la sala
    sesion: str               # token del candidato, o "prueba-..." para un ensayo
    texto: str
    final: bool = False
    monitor_id: str | None = None   # solo en modo prueba, para saber donde anotarla

RAIZ = Path(__file__).resolve().parent.parent.parent
GUION = RAIZ / "prompts" / "consultor_iq_agente.md"
RUBRICA = RAIZ / "prompts" / "consultor_iq.md"

OPENAI_CLIENT_SECRETS = "https://api.openai.com/v1/realtime/client_secrets"

# `mostrar_caso` es lo que hace que el caso aparezca en pantalla y en el chat. El
# guion obliga a llamarla ANTES de leer cada caso: si el modelo la olvidara, el
# candidato escucharia numeros que no puede ver.
HERRAMIENTAS = [
    {
        "type": "function",
        "name": "mostrar_caso",
        "description": (
            "Muestra el caso en la pantalla y lo manda al chat de la reunion. "
            "Se llama SIEMPRE antes de leer un caso en voz alta."
        ),
        "parameters": {
            "type": "object",
            "properties": {"numero": {"type": "integer", "enum": [1, 2]}},
            "required": ["numero"],
        },
    },
    {
        "type": "function",
        "name": "terminar_sesion",
        "description": (
            "Cierra la sesion. Se llama al terminar el cierre, o antes si el "
            "candidato se quiere ir, no hay audio, o la conversacion se sale de "
            "cauce."
        ),
        "parameters": {
            "type": "object",
            "properties": {"motivo": {"type": "string"}},
            "required": ["motivo"],
        },
    },
]


def _casos_para_pantalla() -> list[dict]:
    """Los datos de los dos casos, sacados de la rubrica.

    Se leen de `consultor_iq.md` y no se copian a mano en ningun lado: los
    numeros tienen que tener UNA sola fuente. Si alguien corrige un dato en la
    rubrica, la pantalla y la voz cambian con el.

    Del bloque de cada caso se toma solo el parrafo "Datos que se le dieron:",
    que es literalmente la parte destinada al candidato. Todo lo que sigue
    ("Palanca correcta", "NO cuenta como acierto") es del corrector y se corta.
    """
    texto = RUBRICA.read_text(encoding="utf-8")
    casos = []
    for numero in (1, 2):
        marca = f"### CASO {numero}"
        i = texto.find(marca)
        if i == -1:
            raise RuntimeError(f"No encontre '{marca}' en {RUBRICA.name}")
        fin_titulo = texto.find("\n", i)
        titulo = texto[i + len("### ") : fin_titulo].strip()

        j = texto.find("Datos que se le dieron:", fin_titulo)
        if j == -1:
            raise RuntimeError(f"El {marca} no tiene 'Datos que se le dieron:'")
        # El parrafo termina en la primera linea en blanco.
        k = texto.find("\n\n", j)
        datos = texto[j + len("Datos que se le dieron:") : k].strip()
        datos = " ".join(datos.split()).replace("**", "")

        # Red de seguridad: si un dia alguien reordena la rubrica y la respuesta
        # queda dentro del parrafo de datos, esto revienta aca y no en la cara
        # del candidato.
        bajo = datos.lower()
        if "palanca correcta" in bajo or "cuenta como acierto" in bajo:
            raise RuntimeError(
                f"Los datos del caso {numero} arrastran la respuesta correcta. "
                "Revisar el formato de consultor_iq.md antes de seguir."
            )

        # En pantalla los datos entran como lista: un dato por renglon se lee de
        # un vistazo mientras la persona habla, un parrafo corrido no.
        lineas = [x.strip(" .") for x in datos.split(";") if x.strip(" .")]

        casos.append(
            {
                "numero": numero,
                "titulo": titulo.replace("CASO", "Caso"),
                "datos": datos,
                "lineas": lineas,
            }
        )
    return casos


def _verificar_token(t: str | None) -> None:
    """Puerta simple: crear una clave efimera gasta plata y la URL es publica.

    Sin `IQ_SALA_TOKEN` configurado la sala queda abierta, que es lo comodo para
    probarla en local. En Railway la variable tiene que estar seteada.
    """
    esperado = settings.IQ_SALA_TOKEN
    if not esperado:
        return
    if t != esperado:
        raise HTTPException(status_code=403, detail="Token de sala invalido")


@router.get("/casos")
async def casos(t: str | None = Query(None)):
    """Los dos casos, para que la pagina los muestre. Sin respuestas."""
    _verificar_token(t)
    return {"casos": _casos_para_pantalla()}


@router.post("/clave")
async def crear_clave(t: str | None = Query(None), n: str | None = Query(None)):
    """Crea la clave efimera de OpenAI Realtime, ya atada al guion y a la voz."""
    _verificar_token(t)

    if not settings.OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="Falta OPENAI_API_KEY")
    if not GUION.exists():
        raise HTTPException(status_code=500, detail=f"Falta {GUION.name}")

    instrucciones = GUION.read_text(encoding="utf-8")
    if n:
        # El saludo por el nombre se agrega aca y no en el navegador: las
        # instrucciones nunca bajan al cliente.
        instrucciones += (
            "\n\n## EL CANDIDATO DE ESTA SESION\n\n"
            f"Se llama {n}. Saludalo por su nombre al abrir."
        )

    cuerpo = {
        "session": {
            "type": "realtime",
            "model": settings.IQ_MODELO_VOZ,
            "instructions": instrucciones,
            "audio": {
                "input": {
                    # Sin esto no hay transcripcion de lo que dice el candidato,
                    # que es exactamente lo unico que despues se puntua.
                    "transcription": {"model": "gpt-4o-transcribe"},
                    "turn_detection": {"type": "semantic_vad"},
                },
                "output": {"voice": settings.IQ_VOZ},
            },
            "tools": HERRAMIENTAS,
        }
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            OPENAI_CLIENT_SECRETS,
            headers={
                "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=cuerpo,
        )

    if r.status_code >= 400:
        log.error(f"OpenAI rechazo la clave efimera: {r.status_code} {r.text[:500]}")
        raise HTTPException(
            status_code=502, detail=f"OpenAI {r.status_code}: {r.text[:300]}"
        )

    data = r.json()
    log.info(f"Clave efimera creada (modelo {settings.IQ_MODELO_VOZ}, voz {settings.IQ_VOZ})")
    return {
        "clave": data.get("value"),
        "expira": data.get("expires_at"),
        "modelo": settings.IQ_MODELO_VOZ,
        "casos": _casos_para_pantalla(),
    }


@router.post("/transcripcion")
async def guardar_transcripcion(req: TranscripcionRequest):
    """Guarda lo que se dijo en la sesion. La manda la propia sala, en vivo.

    Por que no se usa la transcripcion de Recall ni un webhook: OpenAI Realtime
    ya emite quien dijo cada cosa mientras la sesion ocurre, y la rubrica
    NECESITA esa separacion -- tiene la regla de no darle credito al candidato
    por lo que dijo el entrevistador. Ademas sale gratis y no hay que verificar
    firmas de nadie.

    Se recibe de a pedazos y no solo al final: si el bot se cae en el minuto 12,
    lo dicho hasta ahi ya esta guardado y la sesion se puede corregir igual.
    """
    _verificar_token(req.t)

    # Modo prueba: una sesion de ensayo, sin candidato detras. La transcripcion
    # va al registro de actividad del monitor en vez de a una ficha, asi se puede
    # probar la sala de punta a punta sin marcar a nadie real y sin depender de
    # que la migracion 005 este corrida.
    if req.sesion.startswith("prueba-"):
        db.log_activity(
            req.monitor_id or "",
            "iq_prueba",
            f"Sesion de prueba {req.sesion}: {len(req.texto)} caracteres"
            + (" (final)" if req.final else ""),
            {"transcripcion": req.texto, "final": req.final},
        )
        return {"ok": True, "prueba": True, "guardado": len(req.texto)}

    r = (
        db.get_db()
        .table("candidates")
        .select("id,name,monitor_id,iq_status")
        .eq("iq_session_token", req.sesion)
        .limit(1)
        .execute()
    )
    if not r.data:
        raise HTTPException(status_code=404, detail="Sesion desconocida")
    c = r.data[0]

    cambios: dict = {"iq_transcript": req.texto, "iq_source_kind": "recall"}

    if req.final:
        # Recien con la sesion cerrada entra a la cola de evaluacion. Antes no:
        # se estaria puntuando una conversacion a medias.
        cambios.update(
            {
                "iq_status": "pending",
                "iq_bot_status": "terminado",
                "attempts": 0,
                "worker_id": None,
                "lease_expires_at": db.EPOCH,
            }
        )
        db.log_activity(
            c["monitor_id"],
            "iq_sesion_terminada",
            f"{c.get('name')}: sesion cerrada ({len(req.texto)} caracteres), a la cola de correccion",
        )
        log.info(f"{c.get('name')}: sesion terminada, {len(req.texto)} chars")

    db.update_candidate(c["id"], cambios)
    return {"ok": True, "guardado": len(req.texto), "final": req.final}
