import asyncio
import json

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from app.worker.manager import worker_manager

router = APIRouter()


@router.get("/{monitor_id}/events")
async def event_stream(monitor_id: str):
    queue = worker_manager.subscribe(monitor_id)

    async def generate():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30)
                    yield {"event": event.get("type", "update"), "data": json.dumps(event)}
                except asyncio.TimeoutError:
                    # Send keepalive ping to prevent connection drop
                    yield {"event": "ping", "data": "{}"}
        except asyncio.CancelledError:
            pass
        finally:
            worker_manager.unsubscribe(monitor_id, queue)

    return EventSourceResponse(generate())
