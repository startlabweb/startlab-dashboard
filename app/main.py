import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.api import monitors, criteria, candidates, sheets, events, health, iq_agente, iq_sesion
from app.worker.manager import worker_manager

log = logging.getLogger("app.main")

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(f"Templates dir: {TEMPLATES_DIR} (exists: {TEMPLATES_DIR.exists()})")
    log.info(f"Static dir: {STATIC_DIR} (exists: {STATIC_DIR.exists()})")
    # El worker solo arranca si este proceso tiene ese rol. Con ROLE=web el
    # dashboard no procesa nada, y el worker vive en otro servicio de Railway.
    corre_worker = settings.ROLE in ("all", "worker")

    if settings.SUPABASE_URL and corre_worker:
        try:
            # Restos de descargas de corridas anteriores. Sin esto, dos o tres
            # muertes subitas llenan el disco (5 GB en el plan Hobby) y TODOS los
            # jobs empiezan a fallar por ENOSPC.
            from tools.loom_downloader import cleanup_tmp_root

            borrados = cleanup_tmp_root()
            if borrados:
                log.info(f"Limpieza de temporales: {borrados} restos borrados")
        except Exception as e:
            log.warning(f"No se pudo limpiar los temporales: {e}")

        try:
            # Guardar las referencias: asyncio solo mantiene referencias debiles a
            # las tasks, asi que una task sin referencia fuerte puede ser
            # recolectada y cancelada en silencio. El watchdog podia desaparecer.
            worker_manager._boot_task = asyncio.create_task(
                worker_manager.restore_active_monitors()
            )
            worker_manager._watchdog_task = asyncio.create_task(
                worker_manager._watchdog_loop()
            )
        except Exception as e:
            log.error(f"Error restoring monitors: {e}")
    elif not corre_worker:
        log.info(f"ROLE={settings.ROLE}: este proceso no procesa candidatos")

    yield
    # Shutdown: stop all monitors
    if corre_worker:
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
app.include_router(iq_agente.router, prefix="/api/iq", tags=["iq"])
# El link del correo del candidato. Va bajo /iq y no /api: lo abre una persona.
app.include_router(iq_sesion.router, prefix="/iq", tags=["iq"])


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


@app.get("/iq/sala", response_class=HTMLResponse)
async def iq_sala_page(request: Request):
    """La sala del Business IQ Test.

    Esta pagina ES el entrevistador: habla con OpenAI Realtime por WebRTC y
    muestra el caso en pantalla. Cuando entre Recall, el bot va a abrir esta
    misma URL y transmitir su audio y su video a la reunion de Zoom. Mientras
    tanto se prueba abriendola en un navegador y hablandole.
    """
    return templates.TemplateResponse(request=request, name="iq_sala.html")
