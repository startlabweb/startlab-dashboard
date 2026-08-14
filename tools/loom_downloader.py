"""Descarga de Loom, solo audio, en un directorio unico por invocacion.

Dos problemas del diseño anterior que esto resuelve:

1. **Cruce de audio entre candidatos.** Los temporales se llamaban
   `video_{row_number}.mp4` en un directorio compartido, y el fallback hacia
   `glob("video_{row}.*")` tomando `candidates[0]`. Dos monitores con la misma
   fila (o un huerfano de una corrida anterior) hacian que un candidato se
   evaluara con el audio de otro. No fallaba: puntuaba mal.

2. **Disco.** Se bajaba el video completo (112 MB medidos para 5 min) y despues
   se extraia el audio con ffmpeg. Loom expone streams de audio separados
   (`hls-raw-audio-audio`), asi que yt-dlp puede bajar solo el audio: 7 MB
   medidos. En el plan Hobby de Railway hay 5 GB de disco.
"""

import os
import shutil
import subprocess
import uuid
from pathlib import Path

from tools.logger import get_logger

log = get_logger("loom_downloader")

# Fuera del repo: en Railway el FS del contenedor es efimero y /tmp es lo correcto.
TMP_ROOT = Path(os.getenv("TMP_ROOT", "/tmp/startlab"))

TIMEOUT_SEGUNDOS = 240


def download_loom(url: str, job_key: str | int, extract_audio: bool = True) -> Path:
    """Baja SOLO el audio de un Loom a un directorio propio de esta invocacion.

    Args:
        url: link de Loom (share o embed).
        job_key: identificador del job, solo para los logs. El aislamiento no
            depende de el: cada llamada usa un directorio con un uuid nuevo.
        extract_audio: se ignora en el camino de Loom — yt-dlp ya devuelve audio.
            Se mantiene en la firma por compatibilidad con el call site.

    Returns:
        Path al archivo de audio. El llamador debe pasarlo a `cleanup_download`.
    """
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    workdir = TMP_ROOT / uuid.uuid4().hex
    workdir.mkdir()

    cmd = [
        "yt-dlp",
        "-f", "bestaudio/best",          # Loom expone streams de audio separados
        "-x", "--audio-format", "m4a",
        "--audio-quality", "5",
        "--no-playlist",
        "--no-warnings",
        "--no-progress",
        "--no-cache-dir",
        "--no-part",
        "--retries", "3",
        "--fragment-retries", "10",
        "--socket-timeout", "20",
        "--concurrent-fragments", "4",
        "--max-filesize", "300M",
        "-o", str(workdir / "a.%(ext)s"),
        url,
    ]

    log.info(f"Job {job_key}: bajando audio de Loom...")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=TIMEOUT_SEGUNDOS
        )
    except subprocess.TimeoutExpired:
        cleanup_download(workdir)
        raise RuntimeError(
            f"yt-dlp supero el timeout de {TIMEOUT_SEGUNDOS}s"
        ) from None

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        cleanup_download(workdir)
        log.error(f"Job {job_key}: yt-dlp fallo: {stderr[:300]}")
        raise RuntimeError(f"yt-dlp failed: {stderr[:300]}")

    # El directorio es privado de esta invocacion: no hay ambiguedad posible.
    archivos = sorted(p for p in workdir.iterdir() if p.is_file())
    if len(archivos) != 1:
        cleanup_download(workdir)
        raise RuntimeError(
            f"yt-dlp dejo {len(archivos)} archivos en vez de 1 "
            f"({[p.name for p in archivos]})"
        )

    audio = archivos[0]
    size_mb = round(audio.stat().st_size / (1024 * 1024), 1)
    log.info(f"Job {job_key}: audio listo {audio.name} ({size_mb} MB)")
    return audio


def cleanup_download(path: Path | None) -> None:
    """Borra el directorio de trabajo completo. Idempotente, nunca levanta.

    Acepta el archivo de audio o el directorio: si le pasan el archivo, borra el
    directorio que lo contiene (que es el workdir con uuid).
    """
    if path is None:
        return
    try:
        objetivo = path if path.is_dir() else path.parent
        # Guarda: solo borramos dentro de TMP_ROOT, nunca fuera.
        if TMP_ROOT.resolve() not in objetivo.resolve().parents:
            log.warning(f"cleanup_download ignorado, fuera de TMP_ROOT: {objetivo}")
            return
        shutil.rmtree(objetivo, ignore_errors=True)
    except Exception as e:
        log.warning(f"cleanup_download no pudo borrar {path}: {e}")


def cleanup_tmp_root() -> int:
    """Borra restos de corridas anteriores. Se llama al arrancar el worker.

    Sin esto, dos o tres muertes subitas con descargas en vuelo llenan el disco
    y TODOS los jobs empiezan a fallar por ENOSPC — el modo de falla mas dificil
    de diagnosticar.

    Returns:
        Cantidad de directorios borrados.
    """
    if not TMP_ROOT.exists():
        return 0
    borrados = 0
    for hijo in TMP_ROOT.iterdir():
        try:
            if hijo.is_dir():
                shutil.rmtree(hijo, ignore_errors=True)
            else:
                hijo.unlink(missing_ok=True)
            borrados += 1
        except Exception as e:
            log.warning(f"No se pudo borrar {hijo}: {e}")
    if borrados:
        log.info(f"cleanup_tmp_root: {borrados} restos borrados de {TMP_ROOT}")
    return borrados
