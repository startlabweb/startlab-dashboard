import subprocess
from pathlib import Path

from googleapiclient.http import MediaIoBaseDownload

from tools.drive_metadata import get_drive_service
from tools.logger import get_logger

log = get_logger("drive_downloader")

TMP_DIR = Path(__file__).resolve().parent.parent / ".tmp"


def _extract_audio(video_path: Path, row_number: int) -> Path:
    audio_path = video_path.parent / f"audio_{row_number}.mp3"

    cmd = [
        "ffmpeg", "-i", str(video_path),
        "-vn",
        "-acodec", "libmp3lame",
        "-ab", "64k",
        "-ar", "16000",
        "-ac", "1",
        "-y",
        str(audio_path),
    ]

    log.info(f"Row {row_number}: extracting audio...")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    if result.returncode != 0:
        log.error(f"Row {row_number}: ffmpeg error: {result.stderr[:200]}")
        raise RuntimeError(f"ffmpeg failed: {result.stderr[:200]}")

    size_mb = round(audio_path.stat().st_size / (1024 * 1024), 1)
    log.info(f"Row {row_number}: audio extracted {audio_path.name} ({size_mb} MB)")
    return audio_path


def download_video(file_id: str, row_number: int, extract_audio: bool = True) -> Path:
    TMP_DIR.mkdir(exist_ok=True)
    dest = TMP_DIR / f"video_{row_number}.mp4"

    service = get_drive_service()
    request = service.files().get_media(fileId=file_id)

    with open(dest, "wb") as f:
        downloader = MediaIoBaseDownload(f, request, chunksize=50 * 1024 * 1024)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                log.info(f"Row {row_number}: download {int(status.progress() * 100)}%")

    size_mb = round(dest.stat().st_size / (1024 * 1024), 1)
    log.info(f"Row {row_number}: downloaded {dest.name} ({size_mb} MB)")

    if extract_audio:
        audio_path = _extract_audio(dest, row_number)
        dest.unlink()
        return audio_path

    return dest
