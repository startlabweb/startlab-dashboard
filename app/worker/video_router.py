import re


def detect_video_source(url: str | None) -> tuple[str, str | None]:
    """Detect video source from URL.

    Returns:
        (source_type, identifier) where source_type is 'google_drive', 'loom', or 'none'
    """
    if not url or not url.strip():
        return ("none", None)

    url = url.strip()

    # Google Drive: /d/FILE_ID or id=FILE_ID
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", url)
    if m:
        return ("google_drive", m.group(1))
    m = re.search(r"id=([a-zA-Z0-9_-]+)", url)
    if m:
        return ("google_drive", m.group(1))

    # Loom: loom.com/share/VIDEO_ID
    m = re.search(r"loom\.com/share/([a-zA-Z0-9]+)", url)
    if m:
        return ("loom", url)

    # Unknown URL format — try yt-dlp as fallback
    if url.startswith("http"):
        return ("loom", url)  # yt-dlp can handle most video URLs

    return ("none", None)
