"""Descarga de videos desde Google Drive: solo el audio, sin video en disco.

Reescrito con el mismo patron ya probado del loom_downloader, porque el intake
principal paso de Loom a links de Drive y este camino tenia los defectos que ya
habiamos arreglado en el otro:

1. **Cruce de audios**: guardaba en <repo>/.tmp/video_{fila}.mp4 — directorio
   compartido + nombre por numero de fila = dos jobs podian pisarse el archivo y
   un candidato recibia la nota de otro, sin error visible.
2. **El video completo pasaba por disco**: hasta 1.5 GB medidos en la edicion
   pasada, contra 5 GB de disco en Railway con concurrencia 2.
3. **Sin timeout**: el loop de descarga podia colgarse indefinidamente.

Diseño nuevo:

- Via principal: **ffmpeg leyendo directo de la URL autenticada de Drive** y
  extrayendo el audio al vuelo. El video nunca toca el disco; quedan ~7 MB de mp3.
  Drive soporta Range y ffmpeg sabe hacer seek HTTP, asi que los .mov con el
  indice al final tambien funcionan.
- Fallback: descarga chunked (como antes) + extraccion con ffmpeg, pero dentro de
  un directorio aislado por invocacion y con deadline.
- Formatos: ffmpeg resuelve por contenido, no por extension — mp4, mov, webm,
  mkv, avi, 3gp y audio puro salen todos como mp3, que el pipeline ya consume.
"""

import subprocess
import time
import uuid
from pathlib import Path

from google.auth.transport.requests import Request
from googleapiclient.http import MediaIoBaseDownload

from tools.drive_metadata import get_drive_service
from tools.logger import get_logger
# Misma raiz temporal y misma limpieza que el camino de Loom: una sola
# infraestructura de temporales para todo el worker.
from tools.loom_downloader import TMP_ROOT, cleanup_download  # noqa: F401

log = get_logger("drive_downloader")

TIMEOUT_SEGUNDOS = 300


def _via_transcode_audio(file_id: str, workdir: Path, job_key) -> Path | None:
    """Via MAS rapida: la pista de SOLO AUDIO que Drive genera al transcodificar.

    Drive procesa los videos subidos igual que YouTube y expone formatos
    alternativos; entre ellos hay uno de audio puro (m4a, ~3-16 MB medidos en
    videos reales de 100-700 MB). yt-dlp sabe extraerlo de archivos compartidos
    como "cualquiera con el enlace".

    Devuelve el audio o None para caer a la siguiente via. Motivos tipicos de
    None: el video es muy reciente y Drive todavia no lo transcodifico, o el
    archivo no es publico (solo compartido con la cuenta de servicio).
    """
    url = f"https://drive.google.com/file/d/{file_id}/view"
    cmd = [
        "yt-dlp",
        # bestaudio SIN "/best": si no hay pista de audio, que falle y caigamos
        # al streaming — "best" bajaria el video entero a disco, justo lo que
        # este modulo existe para evitar.
        "-f", "bestaudio",
        "--no-playlist", "--no-warnings", "--no-progress", "--no-cache-dir",
        "--no-part",
        "--retries", "2",
        "--socket-timeout", "20",
        "-o", str(workdir / "a.%(ext)s"),
        url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        log.info(f"Job {job_key}: transcode-audio supero 120s, voy al streaming")
        return None

    if result.returncode != 0:
        log.info(
            f"Job {job_key}: sin pista de audio transcodificada "
            f"({(result.stderr or '').strip()[:120]}); voy al streaming"
        )
        return None

    archivos = [p for p in workdir.iterdir() if p.is_file()]
    if len(archivos) != 1 or archivos[0].stat().st_size < 1024:
        for p in archivos:
            p.unlink(missing_ok=True)
        return None
    return archivos[0]


def _token_de_acceso(service) -> str:
    """Access token vigente del service account, refrescandolo si hace falta."""
    creds = service._http.credentials
    if not creds.valid:
        creds.refresh(Request())
    return creds.token


def _extraer_audio_streaming(file_id: str, workdir: Path, job_key) -> Path | None:
    """Via principal: ffmpeg lee la URL de Drive y extrae el audio al vuelo.

    Devuelve el path del mp3, o None si fallo (el llamador cae al fallback).
    """
    service = get_drive_service()
    token = _token_de_acceso(service)
    url = (
        f"https://www.googleapis.com/drive/v3/files/{file_id}"
        f"?alt=media&supportsAllDrives=true"
    )
    audio = workdir / "a.mp3"
    cmd = [
        "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        # el \r\n final es obligatorio: ffmpeg pasa el bloque de headers crudo
        "-headers", f"Authorization: Bearer {token}\r\n",
        "-i", url,
        "-vn",
        "-acodec", "libmp3lame",
        "-ab", "64k",
        "-ar", "16000",
        "-ac", "1",
        "-y",
        str(audio),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=TIMEOUT_SEGUNDOS
        )
    except subprocess.TimeoutExpired:
        log.warning(f"Job {job_key}: streaming supero {TIMEOUT_SEGUNDOS}s")
        return None

    if result.returncode != 0 or not audio.exists() or audio.stat().st_size < 1024:
        stderr = (result.stderr or "").strip()[:200]
        log.warning(f"Job {job_key}: streaming fallo ({stderr}); voy al fallback")
        audio.unlink(missing_ok=True)
        return None

    return audio


def _descargar_completo(file_id: str, workdir: Path, job_key) -> Path:
    """Fallback: descarga chunked del archivo entero, con deadline."""
    service = get_drive_service()
    destino = workdir / "video.bin"  # ffmpeg identifica el formato por contenido
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)

    limite = time.monotonic() + TIMEOUT_SEGUNDOS
    with open(destino, "wb") as f:
        downloader = MediaIoBaseDownload(f, request, chunksize=50 * 1024 * 1024)
        done = False
        while not done:
            if time.monotonic() > limite:
                raise RuntimeError(
                    f"La descarga supero el timeout de {TIMEOUT_SEGUNDOS}s"
                )
            status, done = downloader.next_chunk()
            if status:
                log.info(f"Job {job_key}: descarga {int(status.progress() * 100)}%")

    size_mb = round(destino.stat().st_size / (1024 * 1024), 1)
    log.info(f"Job {job_key}: video descargado ({size_mb} MB), extrayendo audio...")
    return destino


def _extract_audio(video_path: Path, job_key) -> Path:
    """Extrae el audio de un video local. Usado por el fallback (y por Loom legacy)."""
    audio_path = video_path.parent / "a.mp3"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", str(video_path),
        "-vn",
        "-acodec", "libmp3lame",
        "-ab", "64k",
        "-ar", "16000",
        "-ac", "1",
        "-y",
        str(audio_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {(result.stderr or '')[:200]}")

    size_mb = round(audio_path.stat().st_size / (1024 * 1024), 1)
    log.info(f"Job {job_key}: audio extraido ({size_mb} MB)")
    return audio_path


def download_video(file_id: str, job_key: str | int, extract_audio: bool = True) -> Path:
    """Obtiene el AUDIO de un video de Drive, en un directorio propio del job.

    Args:
        file_id: id del archivo en Drive (ya validado como accesible).
        job_key: identificador para los logs (candidate_id). El aislamiento no
            depende de el: cada invocacion usa un directorio con uuid propio.
        extract_audio: se mantiene por compatibilidad; siempre se devuelve audio.

    Returns:
        Path al mp3. El llamador debe pasarlo a `cleanup_download` al terminar.
    """
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    workdir = TMP_ROOT / uuid.uuid4().hex
    workdir.mkdir()

    # Via 1: pista de solo audio transcodificada (3-16 MB, la mas rapida)
    audio = _via_transcode_audio(file_id, workdir, job_key)
    if audio is not None:
        size_mb = round(audio.stat().st_size / (1024 * 1024), 1)
        log.info(f"Job {job_key}: audio listo via transcode ({size_mb} MB)")
        return audio

    # Via 2: ffmpeg leyendo el original al vuelo (el video no toca el disco)
    log.info(f"Job {job_key}: extrayendo audio de Drive {file_id[:12]}... (streaming)")
    audio = _extraer_audio_streaming(file_id, workdir, job_key)
    if audio is not None:
        size_mb = round(audio.stat().st_size / (1024 * 1024), 1)
        log.info(f"Job {job_key}: audio listo via streaming ({size_mb} MB)")
        return audio

    # Via 3: descarga completa + extraccion local (ultimo recurso)
    try:
        video = _descargar_completo(file_id, workdir, job_key)
        audio = _extract_audio(video, job_key)
        video.unlink(missing_ok=True)  # el video no se queda en disco
        return audio
    except Exception:
        cleanup_download(workdir)
        raise
