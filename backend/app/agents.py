"""One private agent per repository.

An agent is the unit of work and of context: it runs one task at a time for its
repository, and it keeps its own notes about that repository (`agent_memory`),
which are the only notes ever placed in a prompt about that repository. Agents
for different repositories run in parallel and share nothing.
"""

from __future__ import annotations

import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from . import db, ui_events

_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="agent")
_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()
_futures: list = []

MEMORY_LIMIT = 24  # newest notes kept in a prompt


def _lock_for(repo_id: str) -> threading.Lock:
    with _guard:
        return _locks.setdefault(repo_id, threading.Lock())


def _row(repo_id: str) -> dict[str, Any]:
    rows = db.select("agents", {"repo_id": repo_id}, order="updated_at DESC", limit=1)
    if rows:
        return rows[0]
    return db.insert("agents", {"id": db.new_id("agt"), "repo_id": repo_id, "status": "idle", "task": None, "detail": None,
                                "migration_id": None, "data": {}, "started_at": None, "updated_at": db.now()})


def public(agent: dict[str, Any]) -> dict[str, Any]:
    repo = db.get("repos", agent["repo_id"]) or {}
    return {"id": agent["id"], "repoId": agent["repo_id"], "repoName": repo.get("name"), "status": agent["status"], "task": agent["task"],
            "detail": agent["detail"], "migrationId": agent["migration_id"], "startedAt": agent["started_at"], "updatedAt": agent["updated_at"],
            "memories": len(db.select("agent_memory", {"repo_id": agent["repo_id"]}))}


def list_agents() -> list[dict[str, Any]]:
    return [public(_row(repo["id"])) for repo in db.select("repos", order="created_at ASC")]


def set_state(repo_id: str, status: str, task: str | None = None, detail: str | None = None, migration_id: str | None = None) -> None:
    """status: idle | working | waiting (needs the user) ."""
    agent = _row(repo_id)
    values: dict[str, Any] = {"status": status, "detail": detail, "updated_at": db.now()}
    if task is not None or status == "idle":
        values["task"] = task
    if migration_id is not None or status == "idle":
        values["migration_id"] = migration_id
    if status == "working" and agent["status"] != "working":
        values["started_at"] = db.now()
    db.update("agents", agent["id"], values)
    ui_events.broadcast({"t": "agent", "repoId": repo_id, "agent": public({**agent, **values})})


def progress(repo_id: str, detail: str) -> None:
    agent = _row(repo_id)
    if agent["status"] == "working":
        set_state(repo_id, "working", detail=detail)


def submit(repo_id: str, task: str, fn: Callable[[], Any], *, migration_id: str | None = None) -> None:
    """Queue work for a repository's agent. Tasks for one repository run one after another."""

    def run() -> None:
        with _lock_for(repo_id):
            if db.get("repos", repo_id) is None:
                return  # disconnected while queued
            set_state(repo_id, "working", task, migration_id=migration_id)
            try:
                fn()
            except Exception:
                traceback.print_exc()
            finally:
                waiting = [c for c in db.select("env_changes", {"repo_id": repo_id, "status": "pending"})]
                if waiting:
                    set_state(repo_id, "waiting", f"Waiting for your answer: {waiting[0]['summary']}")
                else:
                    set_state(repo_id, "idle")

    _futures.append(_pool.submit(run))


def drain(timeout: float = 120) -> None:
    """Block until every queued task has finished (tests, scripts)."""
    while _futures:
        _futures.pop(0).result(timeout=timeout)


# --- memory ---------------------------------------------------------------------------


def remember(repo_id: str, kind: str, content: str) -> None:
    """kind: pipeline | outcome | decision | review | convention."""
    if kind == "pipeline":  # the map is replaced, not accumulated
        for old in db.select("agent_memory", {"repo_id": repo_id, "kind": "pipeline"}):
            db.delete("agent_memory", {"id": old["id"]})
    db.insert("agent_memory", {"id": db.new_id("mem"), "repo_id": repo_id, "kind": kind, "content": content.strip(), "created_at": db.now()})


def recall(repo_id: str) -> str | None:
    notes = db.select("agent_memory", {"repo_id": repo_id}, order="created_at DESC", limit=MEMORY_LIMIT)
    if not notes:
        return None
    return "\n".join(f"- [{n['kind']}, {n['created_at'][:10]}] {n['content']}" for n in reversed(notes))


def memories(repo_id: str) -> list[dict[str, Any]]:
    return [{"id": n["id"], "kind": n["kind"], "content": n["content"], "createdAt": n["created_at"]}
            for n in db.select("agent_memory", {"repo_id": repo_id}, order="created_at DESC", limit=100)]
