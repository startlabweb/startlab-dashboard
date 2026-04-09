import os
import time
from pathlib import Path

import google.generativeai as genai

from tools.logger import get_logger

log = get_logger("gemini_uploader")

POLL_INTERVAL = 10
TIMEOUT = 300


def upload_to_gemini(local_path: Path) -> str:
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])

    log.info(f"Uploading {local_path.name} to Gemini File API...")
    mime_types = {
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".wav": "audio/wav",
    }
    mime = mime_types.get(local_path.suffix.lower(), "video/mp4")
    uploaded = genai.upload_file(path=str(local_path), mime_type=mime)
    log.info(f"File uploaded: {uploaded.name}, waiting for processing...")

    elapsed = 0
    while uploaded.state.name == "PROCESSING":
        if elapsed >= TIMEOUT:
            raise TimeoutError(f"Gemini did not finish processing {local_path.name} in {TIMEOUT}s")
        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
        uploaded = genai.get_file(uploaded.name)

    if uploaded.state.name != "ACTIVE":
        raise RuntimeError(f"Gemini unexpected state: {uploaded.state.name}")

    log.info(f"File ready: {uploaded.uri}")
    return uploaded.uri
