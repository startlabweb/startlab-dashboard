"""Motivos legibles para humanos de por que un candidato quedo en N/A o error.

Las razones ya existian en los datos (el texto crudo de la celda del video
queda en `candidates.video_url`, y el error completo en `error_message`), pero
estaban enterradas: una solo en los logs, la otra solo en el modal y el Sheet.
Este modulo las convierte en frases cortas que la tabla puede mostrar.
"""

from urllib.parse import urlparse

# Largo maximo del texto libre que se cita (ej. "no pude hacer el video...");
# el detalle completo siempre queda en el modal del candidato.
MAX_CITA = 140


def resumen_error(mensaje: object) -> str:
    """Reduce un error largo a una etiqueta corta.

    Vivia como `_resumen_corto` en sheet_sync (para la celda de puntaje del
    Sheet); ahora es compartido con la API para que la tabla del dashboard
    muestre el mismo motivo.
    """
    m = (str(mensaje) if mensaje else "").lower()
    if "sin acceso" in m or "cannot access" in m:
        return "sin acceso al video"
    if "pesa" in m and "mb" in m:
        return "video muy pesado"
    if "yt-dlp" in m or "loom" in m or "carpeta" in m:
        return "link roto o de carpeta"
    if "timeout" in m or "supero" in m:
        return "tiempo agotado"
    if "json" in m or "parsear" in m:
        return "transcripcion fallida"
    if "connection" in m or "connect" in m or "network" in m:
        return "fallo de conexion (reintentar)"
    return "ver explicacion"


def razon_sin_video(video_url: object) -> str:
    """Explica por que un candidato quedo sin video (el "N/A" de la tabla).

    `video_url` es la celda cruda del formulario: puede ser una URL valida de
    otro dominio, una carpeta de Drive, texto libre ("no pude hacer el video")
    o estar vacia. `detect_video_url` descarta todos esos casos como "none";
    aca se reconstruye el motivo a partir del mismo texto.
    """
    texto = str(video_url or "").strip()
    if not texto:
        return "No adjunto video (dejo el campo vacio)"
    if not texto.lower().startswith("http"):
        cita = texto if len(texto) <= MAX_CITA else texto[: MAX_CITA - 3] + "..."
        return f'Respondio texto en vez de video: "{cita}"'
    if "drive.google.com" in texto and "/folders/" in texto:
        return "Mando una carpeta de Drive, no el archivo del video"
    dominio = urlparse(texto).netloc or "desconocido"
    return f"Link no soportado ({dominio}): solo Drive o Loom"
