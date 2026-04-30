import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict

from app import database as db

log = logging.getLogger("worker.manager")


class MonitorWorker:
    """Manages polling loops for all active monitors."""

    def __init__(self):
        self.active_tasks: Dict[str, asyncio.Task] = {}
        self._event_subscribers: Dict[str, list] = {}  # monitor_id -> [queues]

    async def start_monitor(self, monitor_id: str):
        if monitor_id in self.active_tasks:
            return
        db.update_monitor(monitor_id, {"status": "active"})
        task = asyncio.create_task(self._poll_loop(monitor_id))
        self.active_tasks[monitor_id] = task
        db.log_activity(monitor_id, "monitor_started", "Monitoreo activado")
        log.info(f"Monitor {monitor_id} started")

    async def stop_monitor(self, monitor_id: str):
        task = self.active_tasks.pop(monitor_id, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        db.update_monitor(monitor_id, {"status": "paused"})
        db.log_activity(monitor_id, "monitor_paused", "Monitoreo pausado")
        log.info(f"Monitor {monitor_id} stopped")

    async def stop_all(self):
        for mid in list(self.active_tasks.keys()):
            await self.stop_monitor(mid)

    async def restore_active_monitors(self):
        """Restart polling for monitors that were active before shutdown."""
        try:
            monitors = db.list_monitors()
            for m in monitors:
                if m.get("status") == "active":
                    log.info(f"Restoring monitor {m['id']}")
                    await self.start_monitor(m["id"])
        except Exception as e:
            log.error(f"Error restoring monitors: {e}")

    def subscribe(self, monitor_id: str) -> asyncio.Queue:
        queue = asyncio.Queue()
        if monitor_id not in self._event_subscribers:
            self._event_subscribers[monitor_id] = []
        self._event_subscribers[monitor_id].append(queue)
        return queue

    def unsubscribe(self, monitor_id: str, queue: asyncio.Queue):
        subs = self._event_subscribers.get(monitor_id, [])
        if queue in subs:
            subs.remove(queue)

    async def _emit_event(self, monitor_id: str, event: dict):
        for queue in self._event_subscribers.get(monitor_id, []):
            await queue.put(event)

    async def _poll_loop(self, monitor_id: str):
        from app.worker.processor import process_new_candidates

        # Capture the running event loop BEFORE entering the thread
        loop = asyncio.get_running_loop()

        while True:
            try:
                monitor = db.get_monitor(monitor_id)
                if not monitor or monitor["status"] != "active":
                    break

                def emit_from_thread(event: dict):
                    """Thread-safe callback that schedules event emission on the main loop."""
                    try:
                        loop.call_soon_threadsafe(
                            asyncio.ensure_future,
                            self._emit_event(monitor_id, event),
                        )
                    except Exception:
                        pass  # SSE is optional, don't break processing

                await asyncio.to_thread(
                    process_new_candidates, monitor, emit_from_thread
                )

                db.update_monitor(monitor_id, {
                    "last_poll_at": datetime.now(timezone.utc).isoformat(),
                })

                poll_interval = monitor.get("poll_interval", 60)
                await asyncio.sleep(poll_interval)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error(f"Monitor {monitor_id} error: {e}")
                # Reset the Supabase client singleton — httpx/httpcore connections
                # can get stuck in invalid states after a transient error, causing
                # every subsequent call to fail. Forcing a fresh client recovers.
                db._client = None
                try:
                    db.log_activity(monitor_id, "error", f"Error en polling: {e}")
                except Exception as log_err:
                    log.error(f"Could not log activity for {monitor_id}: {log_err}")
                await asyncio.sleep(60)

    async def _watchdog_loop(self, check_interval: int = 300):
        """Detects polling tasks that died silently and revives them."""
        while True:
            try:
                await asyncio.sleep(check_interval)
                monitors = db.list_monitors()
                now = datetime.now(timezone.utc)
                for m in monitors:
                    if m.get("status") != "active":
                        continue
                    mid = m["id"]
                    task = self.active_tasks.get(mid)
                    task_dead = task is None or task.done()

                    last_poll = m.get("last_poll_at")
                    poll_interval = m.get("poll_interval") or 60
                    stale = False
                    if last_poll:
                        try:
                            last_dt = datetime.fromisoformat(last_poll.replace("Z", "+00:00"))
                            stale = (now - last_dt).total_seconds() > poll_interval * 5
                        except Exception:
                            pass

                    if task_dead or stale:
                        log.warning(f"Watchdog: reviving monitor {mid} (task_dead={task_dead}, stale={stale})")
                        self.active_tasks.pop(mid, None)
                        try:
                            await self.start_monitor(mid)
                        except Exception as e:
                            log.error(f"Watchdog could not restart {mid}: {e}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error(f"Watchdog error: {e}")
                db._client = None


worker_manager = MonitorWorker()
