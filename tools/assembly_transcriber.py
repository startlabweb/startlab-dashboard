"""Transcripcion con AssemblyAI, devolviendo la MISMA estructura que Gemini.

Por que existe: hoy los datos de fluidez (pausas, muletillas, continuidad) los
produce un modelo de lenguaje, o sea que los *estima*. Eso trajo dos problemas
reales, medidos:

  1. Cuando la respuesta venia mal formada, el codigo de rescate rellenaba
     muletillas=0 y continuidad=3 y los devolvia como si fueran medidos. Un
     candidato recibio 3/6 en fluidez sobre numeros fabricados.
  2. En un caso Gemini se trabo en loop: 20.314 caracteres para 5 minutos de
     audio (~4.000 palabras, imposible), 140 muletillas y 0 de fluidez.

AssemblyAI trae timestamp de cada palabra, asi que las pausas se **calculan** y
las muletillas se **cuentan** sobre el texto. Deja de ser una opinion.

Clave del diseño: devuelve el mismo dict que `gemini_evaluator.evaluate_video`,
asi que el prompt de scoring, la rubrica y el resto del pipeline NO se tocan. Se
elige el motor con la variable TRANSCRIBER y se puede volver atras al instante.

No agrega dependencias: usa la API REST con httpx, que ya viene con supabase.
"""

import os
import re
import time
import unicodedata
from pathlib import Path

import httpx

from tools.logger import get_logger

log = get_logger("assembly_transcriber")

BASE_URL = "https://api.assemblyai.com/v2"
TIMEOUT_SUBIDA = 300
TIMEOUT_POLL = 600
INTERVALO_POLL = 3

# Umbral de pausa larga. Coincide con la definicion del prompt actual:
# "pausas largas (mas de 2 segundos de silencio)".
PAUSA_LARGA_MS = 2000

# Las 6 muletillas que el prompt de scoring desglosa. Se cuentan por palabra
# completa para que "como" no matchee dentro de "comodo".
MULETILLAS = {
    "ehm": [r"\behm+\b", r"\behh+\b", r"\bem+\b", r"\beh\b"],
    "este": [r"\beste\b"],
    "o sea": [r"\bo\s+sea\b"],
    "bueno": [r"\bbueno\b"],
    "como": [r"\bcomo\b"],
    "entonces": [r"\bentonces\b"],
}


def _sin_tildes(texto: str) -> str:
    nfkd = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _contar_muletillas(texto: str) -> tuple[int, dict[str, int]]:
    """Cuenta muletillas por palabra completa. Determinista y reproducible."""
    normalizado = _sin_tildes(texto)
    detalle: dict[str, int] = {}
    for nombre, patrones in MULETILLAS.items():
        n = sum(len(re.findall(p, normalizado)) for p in patrones)
        detalle[nombre] = n
    return sum(detalle.values()), detalle


def _analizar_pausas(palabras: list[dict]) -> tuple[int, str]:
    """Cuenta pausas largas usando los timestamps reales de cada palabra."""
    if len(palabras) < 2:
        return 0, "No hay suficientes palabras para medir pausas"

    huecos = []
    for previa, siguiente in zip(palabras, palabras[1:]):
        hueco = (siguiente.get("start") or 0) - (previa.get("end") or 0)
        if hueco >= PAUSA_LARGA_MS:
            huecos.append((hueco / 1000, (previa.get("end") or 0) / 1000))

    if not huecos:
        return 0, "Sin pausas mayores a 2 segundos"

    huecos.sort(reverse=True)
    ejemplos = ", ".join(
        f"{dur:.1f}s en el minuto {momento/60:.1f}" for dur, momento in huecos[:4]
    )
    return len(huecos), f"{len(huecos)} pausas de mas de 2s. Las mas largas: {ejemplos}"


def _contar_repeticiones(texto: str) -> int:
    """Palabras repetidas de forma inmediata ('el el', 'que que'): señal de titubeo."""
    palabras = re.findall(r"\b\w+\b", _sin_tildes(texto))
    return sum(1 for a, b in zip(palabras, palabras[1:]) if a == b and len(a) > 1)


def _juzgar_continuidad(
    texto: str, pausas: int, pausas_detalle: str, muletillas: int, minutos: float
) -> tuple[int, str]:
    """Juzga la continuidad (1-5) con GPT-4o leyendo la transcripcion.

    Mismo juez que ya evalua el roleplay, con mejor materia prima: la
    transcripcion medida de AssemblyAI (que conserva los "um"/"eh") mas los datos
    de pausas calculados con timestamps reales. Con esto el camino de AssemblyAI
    no depende de Gemini para nada.

    La version anterior era una formula de umbrales fijos (pausas/minuto), y
    estaba mal calibrada: un roleplay es una persona actuando dos voces, y las
    pausas entre turnos son parte del formato, no titubeo. La formula las
    castigaba y hundia la nota de fluidez. Un modelo leyendo el texto distingue
    el cambio de turno del titubeo; una formula no.

    Si el juicio falla, cae a un neutral 3 marcado como estimado.
    """
    import json as _json

    from openai import OpenAI

    prompt = f"""Analiza la CONTINUIDAD del habla en esta transcripcion de un roleplay de ventas.
Es UNA persona actuando dos roles (vendedor y prospecto): las pausas entre turnos
de conversacion son normales y NO cuentan como falta de fluidez. Lo que si cuenta:
titubeos, repeticiones de palabras o frases, oraciones cortadas, muletillas excesivas.

Datos medidos con timestamps reales (no estimados):
- Duracion: {minutos:.1f} minutos
- Pausas de mas de 2 segundos: {pausas} ({pausas_detalle})
- Muletillas contadas en el texto: {muletillas}

Transcripcion literal (conserva los "um", "eh", repeticiones):
---
{texto[:8000]}
---

Devuelve SOLO este JSON, sin markdown:
{{"puntaje": <1 al 5, donde 5 es perfectamente fluido>, "detalle": "<descripcion breve de si el habla es cortada, titubea, repite palabras o frases>"}}"""

    try:
        cliente = OpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=60.0, max_retries=2)
        r = cliente.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.2,
        )
        datos = _json.loads(r.choices[0].message.content.strip())
        puntaje = int(datos.get("puntaje", 3))
        puntaje = max(1, min(5, puntaje))
        return puntaje, str(datos.get("detalle", ""))[:300]
    except Exception as e:
        log.warning(f"Juicio de continuidad fallo ({type(e).__name__}), usando neutral 3")
        return 3, "Estimado (el juicio de continuidad no respondio); revisar si la nota es limite"


def _headers() -> dict:
    key = os.environ.get("ASSEMBLYAI_API_KEY")
    if not key:
        raise RuntimeError("Falta ASSEMBLYAI_API_KEY")
    return {"authorization": key}


def _subir(audio_path: Path) -> str:
    """Sube el audio y devuelve la URL interna de AssemblyAI."""
    with httpx.Client(timeout=TIMEOUT_SUBIDA) as cliente:
        with open(audio_path, "rb") as f:
            r = cliente.post(f"{BASE_URL}/upload", headers=_headers(), content=f.read())
        r.raise_for_status()
        return r.json()["upload_url"]


def _pedir_transcripcion(upload_url: str) -> str:
    cuerpo = {
        "audio_url": upload_url,
        # Deteccion automatica en vez de forzar español: si el candidato manda el
        # roleplay en otro idioma hay que ENTERARSE, no contar 0 muletillas
        # españolas y regalarle los 6 puntos de fluidez. Medido: un audio de
        # prueba estaba en ingles y devolvia 0 muletillas, o sea 6/6 en fluidez.
        "language_detection": True,
        "disfluencies": True,      # conserva las muletillas en el texto
        "punctuate": True,
        "format_text": True,
        "speaker_labels": True,    # separa candidato de prospecto, si se puede
    }
    with httpx.Client(timeout=60) as cliente:
        r = cliente.post(f"{BASE_URL}/transcript", headers=_headers(), json=cuerpo)
        r.raise_for_status()
        return r.json()["id"]


def _esperar(transcript_id: str) -> dict:
    limite = time.monotonic() + TIMEOUT_POLL
    with httpx.Client(timeout=60) as cliente:
        while time.monotonic() < limite:
            r = cliente.get(f"{BASE_URL}/transcript/{transcript_id}", headers=_headers())
            r.raise_for_status()
            datos = r.json()
            estado = datos.get("status")
            if estado == "completed":
                return datos
            if estado == "error":
                raise RuntimeError(f"AssemblyAI: {datos.get('error')}")
            time.sleep(INTERVALO_POLL)
    raise TimeoutError(f"AssemblyAI no termino en {TIMEOUT_POLL}s")


def transcribe(audio_path: Path) -> dict:
    """Transcribe y mide fluidez. Devuelve el mismo dict que Gemini.

    Claves: text, duracion_segundos, pausas, pausas_detalle,
    muletillas{conteo,lista,detalle}, continuidad{puntaje,detalle}, calidad.
    """
    log.info(f"AssemblyAI: subiendo {audio_path.name}...")
    upload_url = _subir(audio_path)

    tid = _pedir_transcripcion(upload_url)
    log.info(f"AssemblyAI: transcripcion {tid} en curso...")
    datos = _esperar(tid)

    texto = datos.get("text") or ""
    palabras = datos.get("words") or []
    dur_ms = datos.get("audio_duration")
    # audio_duration viene en segundos en la API v2; si no, se deriva de la ultima palabra
    if dur_ms:
        segundos = float(dur_ms)
    elif palabras:
        segundos = (palabras[-1].get("end") or 0) / 1000
    else:
        segundos = 0.0
    minutos = segundos / 60 if segundos else 0

    pausas, pausas_detalle = _analizar_pausas(palabras)
    total_mul, desglose = _contar_muletillas(texto)
    repeticiones = _contar_repeticiones(texto)
    cont_puntaje, cont_detalle = _juzgar_continuidad(
        texto, pausas, pausas_detalle, total_mul, minutos
    )

    # Cuantos hablantes detecto, para saber si se puede aislar al candidato
    hablantes = sorted({p.get("speaker") for p in palabras if p.get("speaker")})

    motivos: list[str] = []
    if segundos > 0 and texto:
        cps = len(texto) / segundos
        if cps > 35:
            motivos.append(f"Transcripcion implausible: {cps:.0f} c/s")

    # El conteo de muletillas es en español, pero el proceso acepta videos en
    # español o ingles (ago 2026: el video de presentacion puede grabarse en
    # cualquiera de los dos). Para los idiomas aceptados NO se marca la
    # transcripcion como sospechosa: la rubrica en DB ya instruye como evaluar
    # fillers en ingles leyendo la transcripcion literal.
    idiomas_ok = tuple(
        i.strip() for i in os.environ.get("AUDIO_LANGS_OK", "es,en").split(",") if i.strip()
    )
    idioma = datos.get("language_code")
    if idioma and not str(idioma).startswith(idiomas_ok):
        motivos.append(
            f"El audio no esta en un idioma esperado (detectado: {idioma}, "
            f"aceptados: {', '.join(idiomas_ok)}). El conteo de muletillas no "
            f"aplica y la nota de fluidez NO es confiable"
        )

    calidad: dict = {}
    if motivos:
        calidad = {"transcripcion_sospechosa": True, "motivos": motivos}

    # Un transcript vacio NO se puntua. Medido: un audio devolvio texto vacio y
    # GPT le dio 8/20 con 6/6 en fluidez, mas nota que un roleplay real. Una nota
    # sobre la nada es peor que un error, porque parece valida.
    if not texto.strip():
        raise RuntimeError(
            "AssemblyAI devolvio una transcripcion vacia: no se puede evaluar"
        )

    log.info(
        f"AssemblyAI: {len(texto)} chars, {segundos:.0f}s, {pausas} pausas, "
        f"{total_mul} muletillas, continuidad {cont_puntaje}, "
        f"idioma={idioma}, hablantes={len(hablantes)}"
    )

    return {
        "text": texto,
        "idioma": idioma,
        "duracion_segundos": round(segundos),
        "pausas": pausas,
        "pausas_detalle": pausas_detalle,
        "muletillas": {
            "conteo": total_mul,
            "lista": desglose,
            "detalle": (
                f"Conteo determinista sobre el texto. Repeticiones inmediatas: "
                f"{repeticiones}"
            ),
        },
        "continuidad": {"puntaje": cont_puntaje, "detalle": cont_detalle},
        "calidad": calidad,
        # Extra, no lo consume el prompt pero sirve para diagnosticar
        "_assembly": {
            "hablantes_detectados": len(hablantes),
            "confianza": datos.get("confidence"),
            "palabras": len(palabras),
            "repeticiones": repeticiones,
        },
    }
