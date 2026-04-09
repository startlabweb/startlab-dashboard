import json
import os

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from tools.logger import get_logger

log = get_logger("drive_metadata")

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def get_drive_service():
    sa_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    info = json.loads(sa_json)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)


def get_metadata(file_id: str) -> dict:
    try:
        service = get_drive_service()
        meta = service.files().get(fileId=file_id, fields="id,name,size,mimeType").execute()

        size_bytes = int(meta.get("size", 0))
        max_mb = int(os.environ.get("MAX_VIDEO_SIZE_MB", 500))

        result = {
            "accessible": True,
            "name": meta.get("name", ""),
            "size_bytes": size_bytes,
            "size_mb": round(size_bytes / (1024 * 1024), 1),
            "mime_type": meta.get("mimeType", ""),
            "too_large": size_bytes > max_mb * 1024 * 1024,
        }

        log.info(f"Video '{result['name']}': {result['size_mb']} MB, tipo: {result['mime_type']}")
        return result

    except HttpError as e:
        log.warning(f"No se pudo acceder al video {file_id}: {e.resp.status} {e.reason}")
        return {"accessible": False, "error": f"HTTP {e.resp.status}: {e.reason}"}
    except Exception as e:
        log.error(f"Error inesperado al obtener metadata de {file_id}: {e}")
        return {"accessible": False, "error": str(e)}
