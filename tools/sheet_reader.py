import json
import os
import re
import time

import gspread
from google.oauth2.service_account import Credentials

from tools import sheets_limiter as limiter
from tools.logger import get_logger

log = get_logger("sheet_reader")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


_cliente_cache: gspread.Client | None = None
# (sheet_id, worksheet_name) -> (worksheet, momento_en_que_se_cacheo)
_ws_cache: dict[tuple[str, str], tuple[gspread.Worksheet, float]] = {}
WS_TTL_SEGUNDOS = 300


def get_gspread_client() -> gspread.Client:
    """Cliente de gspread memoizado.

    Antes esto reconstruia las credenciales y re-autorizaba en CADA llamada, lo
    que sumaba un request de token a cada lectura y a cada escritura. Las creds
    del service account son estaticas y google-auth refresca el token solo.
    """
    global _cliente_cache
    if _cliente_cache is None:
        info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        _cliente_cache = gspread.authorize(creds)
    return _cliente_cache


def get_worksheet(
    sheet_id: str, worksheet_name: str = "Form Responses 1"
) -> gspread.Worksheet:
    """Handle de la hoja, cacheado con TTL.

    `open_by_key()` + `worksheet()` son dos requests a Google cada vez. Con 300
    candidatos y dos escrituras por candidato eso eran ~1.200 requests solo para
    resolver el handle, contra un limite de 60/min.
    """
    clave = (sheet_id, worksheet_name)
    en_cache = _ws_cache.get(clave)
    if en_cache is not None:
        ws, cuando = en_cache
        if time.monotonic() - cuando < WS_TTL_SEGUNDOS:
            return ws

    limiter.acquire(cost=2)
    ws = get_gspread_client().open_by_key(sheet_id).worksheet(worksheet_name)
    _ws_cache[clave] = (ws, time.monotonic())
    return ws


def invalidate_sheet_cache(sheet_id: str | None = None) -> None:
    """Tira el cache de handles. Llamar si cambia la estructura del sheet."""
    if sheet_id is None:
        _ws_cache.clear()
        return
    for clave in [k for k in _ws_cache if k[0] == sheet_id]:
        _ws_cache.pop(clave, None)


def extract_sheet_id(url: str) -> str | None:
    """Extract Google Sheet ID from URL."""
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else None


def preview_sheet(
    sheet_id: str,
    worksheet_name: str = "Form Responses 1",
    evaluator_type: str = "sales",
) -> dict:
    """Get sheet metadata + headers + first 3 rows for preview."""
    client = get_gspread_client()
    sheet = client.open_by_key(sheet_id)
    ws = sheet.worksheet(worksheet_name)

    all_values = ws.get_all_values()
    headers = all_values[0] if all_values else []
    sample_rows = all_values[1:4] if len(all_values) > 1 else []
    total_rows = len(all_values) - 1  # exclude header

    # Auto-detect video column (skip for evaluators that don't use video)
    video_column = None
    video_column_index = None
    if evaluator_type == "sales":
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


# Loom: ID de 32 hex. El {16,} deja margen por si Loom cambia el largo.
_RE_LOOM = re.compile(r"loom\.com/(?:share|embed)/([a-fA-F0-9]{16,})")
# Drive: los file_id reales son largos. El {20,} es lo que impide que un UUID
# corto del parametro ?sid= de Loom se confunda con un file_id.
_RE_DRIVE_FILE = re.compile(r"drive\.google\.com/file/d/([a-zA-Z0-9_-]{20,})")
_RE_DRIVE_OPEN = re.compile(r"drive\.google\.com/(?:open|uc)\?[^#]*\bid=([a-zA-Z0-9_-]{20,})")
_RE_DRIVE_FOLDER = re.compile(r"drive\.google\.com/drive/folders/")


def detect_video_url(url: str) -> tuple[str, str | None]:
    """Clasifica la URL del video en ('loom' | 'google_drive' | 'none', id_o_url).

    Loom se evalua PRIMERO y no hay catch-all: lo desconocido devuelve ('none', None).

    Los dos casos que rompian antes:

    1. `loom.com/share/<hex>?sid=<uuid>` — el formato exacto que copia el boton
       "Copy link" de Loom. El regex viejo `id=([a-zA-Z0-9_-]+)` no estaba anclado,
       asi que el substring `sid=` matcheaba y el Loom se clasificaba como
       google_drive con el UUID del sid como file_id -> 404 en la Drive API.
    2. `drive.google.com/drive/folders/...` — un link de carpeta caia en el
       catch-all `startswith("http") -> loom` y terminaba en yt-dlp, que falla.
    """
    if not url or not url.strip():
        return ("none", None)

    url = url.strip()
    if not url.startswith("http"):
        return ("none", None)

    # 1. Loom primero, antes de cualquier patron de Drive
    if _RE_LOOM.search(url):
        return ("loom", url)

    # 2. Carpeta de Drive: no es un archivo, no se puede descargar
    if _RE_DRIVE_FOLDER.search(url):
        log.warning(f"Link de carpeta de Drive, no de archivo: {url[:120]}")
        return ("none", None)

    # 3. Archivo de Drive
    m = _RE_DRIVE_FILE.search(url) or _RE_DRIVE_OPEN.search(url)
    if m:
        return ("google_drive", m.group(1))

    # 4. Loom con un formato que no reconocimos, pero es Loom
    if "loom.com" in url:
        return ("loom", url)

    # 5. Cualquier otra cosa (YouTube, WeTransfer, un Doc, un link roto).
    #    Antes se mandaba a yt-dlp y fallaba 3 veces; ahora se marca y se avisa.
    log.warning(f"URL de video no reconocida: {url[:120]}")
    return ("none", None)


def read_all_rows(sheet_id: str, worksheet_name: str = "Form Responses 1") -> tuple[list[str], list[list[str]]]:
    """Read all rows from sheet. Returns (headers, data_rows).

    Con el handle cacheado esto pasa de ~4 requests a 1.
    """
    ws = get_worksheet(sheet_id, worksheet_name)
    limiter.acquire(cost=1)
    all_values = ws.get_all_values()
    headers = all_values[0] if all_values else []
    data_rows = all_values[1:] if len(all_values) > 1 else []
    return headers, data_rows
