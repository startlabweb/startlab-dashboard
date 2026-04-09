import subprocess
from pathlib import Path

from tools.logger import get_logger

log = get_logger("loom_downloader")

TMP_DIR = Path(__file__).resolve().parent.parent / ".tmp"


def download_loom(url: str, row_number: int, extract_audio: bool = True) -> Path:
    """Download a video from Loom (or any URL) using yt-dlp."""
    TMP_DIR.mkdir(exist_ok=True)
    dest = TMP_DIR / f"video_{row_number}.mp4"

    cmd = [
        "yt-dlp",
        "-o", str(dest),
        "--no-playlist",
        "--merge-output-format", "mp4",
        url,
    ]

    log.info(f"Row {row_number}: downloading from Loom/URL...")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

    if result.returncode != 0:
        log.error(f"Row {row_number}: yt-dlp error: {result.stderr[:300]}")
        raise RuntimeError(f"yt-dlp failed: {result.stderr[:300]}")

    if not dest.exists():
        # yt-dlp sometimes adds extension
        candidates = list(TMP_DIR.glob(f"video_{row_number}.*"))
        if candidates:
            dest = candidates[0]
        else:
            raise RuntimeError(f"yt-dlp did not produce output file for row {row_number}")

    size_mb = round(dest.stat().st_size / (1024 * 1024), 1)
    log.info(f"Row {row_number}: downloaded {dest.name} ({size_mb} MB)")

    if extract_audio:
        from tools.drive_downloader import _extract_audio
        audio_path = _extract_audio(dest, row_number)
        dest.unlink()
        return audio_path

    return dest
