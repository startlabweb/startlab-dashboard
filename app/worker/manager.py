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

        while True:
            try:
                monitor = db.get_monitor(monitor_id)
                if not monitor or monitor["status"] != "active":
                    break

                await asyncio.to_thread(
                    process_new_candidates, monitor, self._emit_event_sync(monitor_id)
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
                db.log_activity(monitor_id, "error", f"Error en polling: {e}")
                await asyncio.sleep(60)

    def _emit_event_sync(self, monitor_id: str):
        """Returns a sync callback that schedules event emission."""
        def callback(event: dict):
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self._emit_event(monitor_id, event))
        return callback


worker_manager = MonitorWorker()
