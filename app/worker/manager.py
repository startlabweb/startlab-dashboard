"""Worker: un loop de ingesta + N loops de drenado por monitor.

Que cambia respecto del diseño anterior y por que:

**El watchdog fabricaba workers duplicados.** `last_poll_at` se escribia recien al
terminar de procesar TODA la tanda, asi que con 300 candidatos quedaba stale por
diseño (el umbral es 5x el poll interval = 300s). El watchdog lo detectaba, hacia
`active_tasks.pop()` **sin cancelar la task previa**, y `start_monitor` creaba una
segunda que pasaba el chequeo de "ya existe" porque el pop la habia borrado. Cada
5 minutos nacia otra, acumulandose.

El arreglo de fondo no es cancelar mejor: es **desacoplar descubrir de procesar**.
El ingest tarda segundos y escribe el heartbeat, asi que `last_poll_at` vuelve a
significar algo. El drenado tarda horas pero no bloquea el heartbeat.

**Lo que se interrumpia se perdia.** Ahora la durabilidad la da la cola con lease
en Postgres (`db.find_claimable` / `claim_candidate`), no el estado en RAM.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict

from app import database as db
from app.config import settings

log = logging.getLogger("worker.manager")


class MonitorWorker:
    """Maneja los loops de cada monitor activo."""

    def __init__(self):
        self.active_tasks: Dict[str, asyncio.Task] = {}
        self._event_subscribers: Dict[str, list] = {}  # monitor_id -> [queues]
        self._watchdog_task: asyncio.Task | None = None
        self._boot_task: asyncio.Task | None = None

    # --- ciclo de vida ---------------------------------------------------

    async def start_monitor(self, monitor_id: str):
        # Antes esto era `if monitor_id in self.active_tasks: return`, sin mirar
        # si la task estaba muerta: si moria, el boton "Activar" del dashboard no
        # hacia nada, y el `pop()` del watchdog era el workaround que causaba las
        # duplicaciones.
        existente = self.active_tasks.get(monitor_id)
        if existente is not None and not existente.done():
            return
        if existente is not None:
            self.active_tasks.pop(monitor_id, None)

        db.update_monitor(monitor_id, {"status": "active"})
        task = asyncio.create_task(self._monitor_loop(monitor_id))
        self.active_tasks[monitor_id] = task
        db.log_activity(monitor_id, "monitor_started", "Monitoreo activado")
        log.info(f"Monitor {monitor_id} started (concurrencia {settings.WORKER_CONCURRENCY})")

    async def stop_monitor(self, monitor_id: str):
        task = self.active_tasks.pop(monitor_id, None)
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        db.update_monitor(monitor_id, {"status": "paused"})
        db.log_activity(monitor_id, "monitor_paused", "Monitoreo pausado")
        log.info(f"Monitor {monitor_id} stopped")

    async def stop_all(self):
        for mid in list(self.active_tasks.keys()):
            await self.stop_monitor(mid)

    async def restore_active_monitors(self):
        """Reanuda los monitores que estaban activos antes del reinicio."""
        try:
            for m in db.list_monitors():
                if m.get("status") == "active":
                    log.info(f"Restoring monitor {m['id']}")
                    await self.start_monitor(m["id"])
        except Exception as e:
            log.error(f"Error restoring monitors: {e}")

    # --- SSE -------------------------------------------------------------

    def subscribe(self, monitor_id: str) -> asyncio.Queue:
        queue = asyncio.Queue()
        self._event_subscribers.setdefault(monitor_id, []).append(queue)
        return queue

    def unsubscribe(self, monitor_id: str, queue: asyncio.Queue):
        subs = self._event_subscribers.get(monitor_id, [])
        if queue in subs:
            subs.remove(queue)

    async def _emit_event(self, monitor_id: str, event: dict):
        for queue in self._event_subscribers.get(monitor_id, []):
            await queue.put(event)

    def _emisor(self, monitor_id: str, loop: asyncio.AbstractEventLoop):
        """Callback thread-safe para emitir eventos desde un thread del worker."""

        def emit_from_thread(event: dict):
            try:
                loop.call_soon_threadsafe(
                    asyncio.ensure_future, self._emit_event(monitor_id, event)
                )
            except Exception:
                pass  # el SSE es opcional, nunca debe romper el procesamiento

        return emit_from_thread

    # --- loops -----------------------------------------------------------

    async def _monitor_loop(self, monitor_id: str):
        """Task padre: un ingest + N drains. Que uno falle no mata a los otros."""
        hijos = [self._ingest_loop(monitor_id)] + [
            self._drain_loop(monitor_id, i)
            for i in range(max(1, settings.WORKER_CONCURRENCY))
        ]
        await asyncio.gather(*hijos, return_exceptions=True)

    async def _ingest_loop(self, monitor_id: str):
        """Descubre filas nuevas y vuelca resultados al Sheet. Barato y rapido."""
        from app.worker.processor import ingest_new_rows
        from tools.sheet_sync import sync_completed_to_sheet

        loop = asyncio.get_running_loop()
        emit = self._emisor(monitor_id, loop)
        ultimo_flush = 0.0

        while True:
            try:
                monitor = await asyncio.to_thread(db.get_monitor, monitor_id)
                if not monitor or monitor["status"] != "active":
                    break

                # Heartbeat AL PRINCIPIO: este ciclo tarda segundos, asi que
                # last_poll_at queda siempre fresco y "stale" vuelve a significar
                # algo. Antes se escribia al final de la tanda completa.
                await asyncio.to_thread(
                    db.update_monitor,
                    monitor_id,
                    {"last_poll_at": datetime.now(timezone.utc).isoformat()},
                )

                info = await asyncio.to_thread(ingest_new_rows, monitor, emit)
                if info.get("new"):
                    log.info(f"Monitor {monitor_id}: {info['new']} filas nuevas encoladas")

                ahora = loop.time()
                if settings.SHEETS_SYNC_ENABLED and (
                    ahora - ultimo_flush >= settings.SHEET_FLUSH_SECONDS
                ):
                    ultimo_flush = ahora
                    try:
                        await asyncio.to_thread(
                            sync_completed_to_sheet,
                            monitor,
                            False,
                            info.get("email_by_row"),
                        )
                    except Exception as e:
                        log.error(f"sheet_sync fallo en {monitor_id}: {e}")

                await asyncio.sleep(settings.POLL_INTERVAL_SECONDS)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error(f"Ingest loop {monitor_id}: {e}")
                db._client = None  # el cliente httpx puede quedar en estado invalido
                await asyncio.sleep(settings.POLL_INTERVAL_SECONDS)

    async def _drain_loop(self, monitor_id: str, slot: int):
        """Toma un candidato de la cola, lo procesa, libera el lease. Uno a la vez."""
        from app.worker.processor import process_one

        loop = asyncio.get_running_loop()
        emit = self._emisor(monitor_id, loop)

        while True:
            try:
                monitor = await asyncio.to_thread(db.get_monitor, monitor_id)
                if not monitor or monitor["status"] != "active":
                    break

                candidato = await asyncio.to_thread(
                    db.find_claimable, monitor_id, settings.MAX_ATTEMPTS
                )
                if not candidato:
                    await asyncio.sleep(5)
                    continue

                reclamado = await asyncio.to_thread(
                    db.claim_candidate, candidato, settings.LEASE_SECONDS
                )
                if not reclamado:
                    # Otro slot o otra replica se lo llevo: seguir, no reintentar.
                    continue

                ok = False
                try:
                    ok = await asyncio.to_thread(
                        process_one, monitor, reclamado, emit
                    )
                except Exception as e:
                    log.error(
                        f"Slot {slot}: fila {reclamado.get('sheet_row')} fallo: {e}"
                    )
                finally:
                    await asyncio.to_thread(
                        db.release_candidate,
                        reclamado["id"],
                        ok,
                        reclamado.get("attempts", 1),
                    )
                    # Heartbeat tambien al terminar cada candidato
                    await asyncio.to_thread(
                        db.update_monitor,
                        monitor_id,
                        {"last_poll_at": datetime.now(timezone.utc).isoformat()},
                    )

            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error(f"Drain loop {monitor_id} slot {slot}: {e}")
                db._client = None
                await asyncio.sleep(30)

    async def _watchdog_loop(self, check_interval: int = 300):
        """Revive monitores cuya task murio. NUNCA arranca una segunda en paralelo."""
        while True:
            try:
                await asyncio.sleep(check_interval)
                monitores = await asyncio.to_thread(db.list_monitors)
                ahora = datetime.now(timezone.utc)
                umbral = settings.POLL_INTERVAL_SECONDS * 5

                for m in monitores:
                    if m.get("status") != "active":
                        continue
                    mid = m["id"]
                    task = self.active_tasks.get(mid)
                    muerta = task is None or task.done()

                    stale = False
                    ultimo = m.get("last_poll_at")
                    if ultimo:
                        try:
                            dt = datetime.fromisoformat(ultimo.replace("Z", "+00:00"))
                            stale = (ahora - dt).total_seconds() > umbral
                        except Exception:
                            pass

                    if not muerta and not stale:
                        continue

                    # Si la task esta VIVA, cancelarla y esperarla antes de
                    # reemplazarla. El bug era hacer pop() sin cancel(): la vieja
                    # seguia corriendo huerfana y se sumaba una nueva.
                    if task is not None and not task.done():
                        log.warning(f"Watchdog: {mid} stale, cancelando la task previa")
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

                    self.active_tasks.pop(mid, None)
                    log.warning(f"Watchdog: reviviendo {mid} (muerta={muerta}, stale={stale})")
                    try:
                        await self.start_monitor(mid)
                    except Exception as e:
                        log.error(f"Watchdog no pudo reiniciar {mid}: {e}")

            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error(f"Watchdog error: {e}")
                db._client = None


worker_manager = MonitorWorker()
