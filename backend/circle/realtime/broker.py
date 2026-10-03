"""In-memory realtime event broker (SSE).

Thread-safe: the watcher/ingestion threads publish, the async SSE endpoint
consumes via asyncio queues.
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, AsyncIterator


class EventBroker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[asyncio.Queue] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self.last_event: dict[str, Any] | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        event = {"type": event_type, "at": time.time(), "data": payload}
        with self._lock:
            self.last_event = event
            queues = list(self._subscribers)
        for q in queues:
            try:
                if self._loop and self._loop.is_running():
                    self._loop.call_soon_threadsafe(q.put_nowait, event)
                else:  # pragma: no cover
                    q.put_nowait(event)
            except RuntimeError:
                pass

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        with self._lock:
            self._subscribers.append(q)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=25.0)
                    yield event
                except asyncio.TimeoutError:
                    yield {"type": "ping", "at": time.time(), "data": {}}
        finally:
            with self._lock:
                if q in self._subscribers:
                    self._subscribers.remove(q)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


broker = EventBroker()
