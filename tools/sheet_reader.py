import json
import os
import re

import gspread
from google.oauth2.service_account import Credentials

from tools.logger import get_logger

log = get_logger("sheet_reader")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


def get_gspread_client() -> gspread.Client:
    sa_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    info = json.loads(sa_json)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def extract_sheet_id(url: str) -> str | None:
    """Extract Google Sheet ID from URL."""
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else None


def preview_sheet(sheet_id: str, worksheet_name: str = "Form Responses 1") -> dict:
    """Get sheet metadata + headers + first 3 rows for preview."""
    client = get_gspread_client()
    sheet = client.open_by_key(sheet_id)
    ws = sheet.worksheet(worksheet_name)

    all_values = ws.get_all_values()
    headers = all_values[0] if all_values else []
    sample_rows = all_values[1:4] if len(all_values) > 1 else []
    total_rows = len(all_values) - 1  # exclude header

    # Auto-detect video column
    video_column = None
    video_column_index = None
    for i, h in enumerate(headers):
        if "video" in h.lower() or "roleplay" in h.lower() or "enlace" in h.lower() or "link" in h.lower():
            if "puntaje" not in h.lower() and "score" not in h.lower():
                video_column = h
                video_column_index = i
                break

    return {
        "title": sheet.title,
        "worksheet": worksheet_name,
        "headers": headers,
        "sample_rows": sample_rows,
        "total_rows": total_rows,
        "video_column": video_column,
        "video_column_index": video_column_index,
    }


def detect_video_url(url: str) -> tuple[str, str | None]:
    """Detect if URL is Google Drive, Loom, or unknown."""
    if not url or not url.strip():
        return ("none", None)

    url = url.strip()

    # Google Drive
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", url)
    if m:
        return ("google_drive", m.group(1))
    m = re.search(r"id=([a-zA-Z0-9_-]+)", url)
    if m:
        return ("google_drive", m.group(1))

    # Loom
    if "loom.com" in url:
        return ("loom", url)

    # Other URL — try yt-dlp
    if url.startswith("http"):
        return ("loom", url)

    return ("none", None)


def read_all_rows(sheet_id: str, worksheet_name: str = "Form Responses 1") -> tuple[list[str], list[list[str]]]:
    """Read all rows from sheet. Returns (headers, data_rows)."""
    client = get_gspread_client()
    sheet = client.open_by_key(sheet_id)
    ws = sheet.worksheet(worksheet_name)
    all_values = ws.get_all_values()
    headers = all_values[0] if all_values else []
    data_rows = all_values[1:] if len(all_values) > 1 else []
    return headers, data_rows
