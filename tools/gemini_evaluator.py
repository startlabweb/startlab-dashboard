import json
import os
import re
import time
from pathlib import Path

import google.generativeai as genai

from tools.logger import get_logger

log = get_logger("gemini_evaluator")

DEFAULT_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "gemini_video_analysis.md"
EXPECTED_KEYS = {"text", "duracion_segundos", "pausas", "muletillas", "continuidad"}

# Sin esto, una llamada colgada bloquea el slot del worker para siempre.
TIMEOUT_SEGUNDOS = 180
MAX_INTENTOS = 3

# Habla normal en español: ~15-19 caracteres por segundo (150-180 palabras/min).
# Arriba de 35 no es humano: es Gemini repitiendo en loop. Medido en un caso real:
# 20.314 caracteres para 296 s = 68,6 c/s, o sea ~4.000 palabras en 5 minutos.
MAX_CHARS_POR_SEGUNDO = 35
MIN_CHARS_POR_SEGUNDO = 2


def _parsear(raw: str) -> dict | None:
    """Intenta sacar el JSON de la respuesta. None si no se puede."""
    limpio = re.sub(r"^```(?:json)?\s*", "", raw)
    limpio = re.sub(r"\s*```$", "", limpio)
    limpio = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", limpio)

    try:
        return json.loads(limpio)
    except json.JSONDecodeError:
        pass

    # Segundo intento: escapar saltos de linea dentro de los strings
    try:
        arreglado = re.sub(
            r'(?<=: ")(.*?)(?=")',
            lambda m: m.group(0).replace("\n", "\\n"),
            limpio,
            flags=re.DOTALL,
        )
        return json.loads(arreglado)
    except json.JSONDecodeError:
        return None


def _extraer_a_mano(raw: str) -> dict | None:
    """Ultimo recurso: saca al menos la transcripcion y la duracion.

    Antes esto rellenaba `pausas`, `muletillas` y `continuidad` con valores
    inventados (0, 0 y 3) y los devolvia como si fueran medidos. El prompt de
    scoring usa esos campos para puntuar "Suena natural y fluido", que vale 6 de
    los 20 puntos: o sea que un candidato recibia una nota de fluidez calculada
    sobre datos fabricados, sin que nadie se enterara. Paso en 1 de 4 pruebas.

    Ahora los campos van igual (el prompt los necesita para renderizar) pero la
    respuesta queda MARCADA, y el processor deja el aviso a la vista.
    """
    limpio = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", raw)
    m_texto = re.search(r'"text"\s*:\s*"(.*?)"(?:\s*,|\s*\})', limpio, re.DOTALL)
    if not m_texto:
        return None
    m_dur = re.search(r'"duracion_segundos"\s*:\s*(\d+)', limpio)
    return {
        "text": m_texto.group(1).replace("\\n", "\n"),
        "duracion_segundos": int(m_dur.group(1)) if m_dur else 0,
        "pausas": 0,
        "pausas_detalle": "No disponible: la respuesta vino mal formada",
        "muletillas": {"conteo": 0, "lista": {}, "detalle": "No disponible"},
        "continuidad": {"puntaje": 3, "detalle": "No disponible"},
        "calidad": {
            "fluidez_estimada": True,
            "motivos": ["Gemini devolvio JSON invalido; los datos de fluidez NO son medidos"],
        },
    }


def _revisar_coherencia(data: dict) -> dict:
    """Marca transcripciones que no pueden ser correctas.

    El caso que se busca: Gemini entrando en loop de repeticion. Devuelve un
    transcript larguisimo con muletillas infladas, y la nota de fluidez sale 0
    aunque el candidato haya hablado bien.
    """
    motivos: list[str] = []
    texto = data.get("text") or ""
    dur = data.get("duracion_segundos") or 0

    if dur > 0 and texto:
        cps = len(texto) / dur
        if cps > MAX_CHARS_POR_SEGUNDO:
            motivos.append(
                f"Transcripcion implausible: {len(texto)} caracteres en {dur:.0f}s "
                f"({cps:.0f} c/s, el habla normal es 15-19). Probable repeticion en loop"
            )
        elif cps < MIN_CHARS_POR_SEGUNDO:
            motivos.append(
                f"Transcripcion demasiado corta: {len(texto)} caracteres en {dur:.0f}s "
                f"({cps:.1f} c/s). Puede ser audio malo o silencio"
            )

    if not texto.strip():
        motivos.append("Transcripcion vacia")

    calidad = data.get("calidad") or {}
    if motivos:
        calidad["transcripcion_sospechosa"] = True
        calidad["motivos"] = list(calidad.get("motivos", [])) + motivos
    data["calidad"] = calidad
    return data


def evaluate_video(file_uri: str, prompt: str | None = None) -> dict:
    """Transcribe y analiza el audio con Gemini.

    Reintenta si la respuesta no se puede parsear, en vez de aceptar de una los
    valores de relleno. Y marca en `calidad` cualquier respuesta dudosa para que
    el resultado no se confunda con una medicion real.
    """
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])

    model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    if prompt is None:
        prompt = DEFAULT_PROMPT_PATH.read_text(encoding="utf-8")

    model = genai.GenerativeModel(model_name=model_name)
    file_ref = (
        genai.get_file(file_uri.split("/")[-1])
        if "/" in file_uri
        else genai.get_file(file_uri)
    )

    ultimo_raw = ""
    for intento in range(1, MAX_INTENTOS + 1):
        log.info(f"Enviando a {model_name} para transcribir (intento {intento}/{MAX_INTENTOS})...")
        try:
            response = model.generate_content(
                [file_ref, prompt],
                request_options={"timeout": TIMEOUT_SEGUNDOS},
            )
            ultimo_raw = (response.text or "").strip()
        except Exception as e:
            log.warning(f"Intento {intento} fallo en la llamada: {type(e).__name__}: {e}")
            if intento < MAX_INTENTOS:
                time.sleep(2 * intento)
                continue
            return {"error": f"Gemini no respondio: {e}"}

        data = _parsear(ultimo_raw)
        if data is not None:
            faltantes = EXPECTED_KEYS - set(data.keys())
            if faltantes:
                log.warning(f"Faltan claves en la respuesta: {faltantes}")

            data = _revisar_coherencia(data)
            calidad = data.get("calidad") or {}

            # Si la transcripcion es implausible, vale la pena reintentar: suele
            # ser un loop puntual del modelo, no un problema del audio.
            if calidad.get("transcripcion_sospechosa") and intento < MAX_INTENTOS:
                log.warning(
                    f"Intento {intento}: {calidad.get('motivos')}. Reintentando..."
                )
                time.sleep(2)
                continue

            log.info(
                f"Transcripcion: {len(data.get('text', ''))} chars, "
                f"duracion: {data.get('duracion_segundos', '?')}s"
                + (f" [REVISAR: {calidad.get('motivos')}]" if calidad else "")
            )
            return data

        log.warning(f"Intento {intento}: JSON invalido")
        if intento < MAX_INTENTOS:
            time.sleep(2 * intento)

    # Se agotaron los reintentos: recien aca se acepta la extraccion a mano,
    # y queda marcada como no medida.
    log.error(f"JSON invalido en los {MAX_INTENTOS} intentos, extrayendo a mano")
    data = _extraer_a_mano(ultimo_raw)
    if data is None:
        return {"error": "No se pudo parsear la respuesta de Gemini", "raw": ultimo_raw[:200]}
    data = _revisar_coherencia(data)
    log.warning(f"Resultado MARCADO para revision: {(data.get('calidad') or {}).get('motivos')}")
    return data
