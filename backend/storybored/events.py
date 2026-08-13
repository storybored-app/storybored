"""In-process pub/sub event bus + the /api/events SSE endpoint.

Event types published across the app: `job`, `shot`, `take`, `character`.
`data` is always the full JSON row of the changed entity.
"""

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from sse_starlette.sse import EventSourceResponse


class EventBus:
    """Fan-out bus. `publish` is safe to call from any thread (request handlers
    run in Starlette's threadpool; the job runner runs on the event loop)."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, event_type: str, data) -> None:
        payload = (event_type, jsonable_encoder(data))
        for queue in list(self._subscribers):
            if self._loop is not None and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._offer, queue, payload)
            else:  # pragma: no cover - no loop yet (startup edge)
                self._offer(queue, payload)

    @staticmethod
    def _offer(queue: asyncio.Queue, payload) -> None:
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:  # slow consumer: drop, never block the app
            pass


router = APIRouter()


@router.get("/api/events")
async def events(request: Request):
    bus: EventBus = request.app.state.bus
    queue = bus.subscribe()

    async def gen():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event_type, data = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    yield {"event": "ping", "data": ""}
                    continue
                yield {"event": event_type, "data": json.dumps(data)}
        finally:
            bus.unsubscribe(queue)

    return EventSourceResponse(gen())
