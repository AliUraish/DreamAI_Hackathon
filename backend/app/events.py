"""Activity feed: every pipeline step is persisted and pushed to live subscribers."""

from __future__ import annotations

import asyncio
from typing import Any

from . import db

_subscribers: set[asyncio.Queue] = set()
_loop: asyncio.AbstractEventLoop | None = None


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def emit(type_: str, message: str, *, repo_id: str | None = None, integration_id: str | None = None,
         migration_id: str | None = None, data: dict[str, Any] | None = None) -> dict[str, Any]:
    event = {"id": db.new_id("evt"), "repo_id": repo_id, "integration_id": integration_id,
             "migration_id": migration_id, "ts": db.now(), "type": type_, "message": message, "data": data or {}}
    db.insert("events", event)
    # The pipeline runs in a worker thread; hand the event to the server's loop.
    if _loop and _loop.is_running():
        for queue in list(_subscribers):
            _loop.call_soon_threadsafe(queue.put_nowait, event)
    return event


def subscribe() -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue()
    _subscribers.add(queue)
    return queue


def unsubscribe(queue: asyncio.Queue) -> None:
    _subscribers.discard(queue)
