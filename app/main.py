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
    # Startup: restart active monitors and launch watchdog
    if settings.SUPABASE_URL:
        try:
            asyncio.create_task(worker_manager.restore_active_monitors())
            asyncio.create_task(worker_manager._watchdog_loop())
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
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/monitors/new", response_class=HTMLResponse)
async def new_monitor_page(request: Request):
    return templates.TemplateResponse(request=request, name="monitor_new.html")


@app.get("/monitors/{monitor_id}", response_class=HTMLResponse)
async def monitor_detail_page(request: Request, monitor_id: str):
    return templates.TemplateResponse(
        request=request, name="monitor_detail.html", context={"monitor_id": monitor_id}
    )
