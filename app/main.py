import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.api import monitors, criteria, candidates, sheets, events, health
from app.worker.manager import worker_manager

log = logging.getLogger("app.main")

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(f"Templates dir: {TEMPLATES_DIR} (exists: {TEMPLATES_DIR.exists()})")
    log.info(f"Static dir: {STATIC_DIR} (exists: {STATIC_DIR.exists()})")
    # Startup: restart active monitors
    if settings.SUPABASE_URL:
        try:
            asyncio.create_task(worker_manager.restore_active_monitors())
        except Exception as e:
            log.error(f"Error restoring monitors: {e}")
    yield
    # Shutdown: stop all monitors
    await worker_manager.stop_all()


app = FastAPI(title="StartLab Dashboard", lifespan=lifespan)

# Static files & templates
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# API routes
app.include_router(health.router)
app.include_router(monitors.router, prefix="/api/monitors", tags=["monitors"])
app.include_router(sheets.router, prefix="/api/sheets", tags=["sheets"])
app.include_router(criteria.router, prefix="/api/monitors", tags=["criteria"])
app.include_router(candidates.router, prefix="/api/monitors", tags=["candidates"])
app.include_router(events.router, prefix="/api/monitors", tags=["events"])


# --- Page routes ---

@app.get("/", response_class=HTMLResponse)
async def dashboard_home(request: Request):
    try:
        return templates.TemplateResponse("index.html", {"request": request})
    except Exception as e:
        log.error(f"Error rendering index.html: {e}")
        return PlainTextResponse(f"Template error: {e}\nTemplates dir: {TEMPLATES_DIR}\nExists: {TEMPLATES_DIR.exists()}\nFiles: {list(TEMPLATES_DIR.glob('*'))}", status_code=500)


@app.get("/monitors/new", response_class=HTMLResponse)
async def new_monitor_page(request: Request):
    return templates.TemplateResponse("monitor_new.html", {"request": request})


@app.get("/monitors/{monitor_id}", response_class=HTMLResponse)
async def monitor_detail_page(request: Request, monitor_id: str):
    return templates.TemplateResponse(
        "monitor_detail.html", {"request": request, "monitor_id": monitor_id}
    )
