from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import audit, db, events, prs, service, traffic, ui_events
from .config import settings
from .routers.api import router


async def _poll_forever() -> None:
    while True:
        await asyncio.sleep(settings.poll_interval_seconds)
        for integration in db.select("integrations"):
            if integration["endpoints"]:
                result = await asyncio.to_thread(service.check_integration, integration["id"], trigger="poll")
                if result["created"]:
                    service.start_migration(result["migration_id"])


async def _watch_env_forever() -> None:
    """Re-read connected repos' env files. Finding a change only raises a question; nothing runs until the user confirms."""
    from .routers.api import check_env_and_announce
    while True:
        await asyncio.sleep(settings.env_watch_seconds)
        for repo in db.select("repos"):
            try:
                await asyncio.to_thread(check_env_and_announce, repo)
            except Exception as exc:  # a deleted checkout must not stop the watcher
                print(f"env watch failed for {repo['name']}: {exc}")


async def _watch_prs_forever() -> None:
    """Open pull requests: review comments become more work for the agent, merges get confirmed."""
    while True:
        await asyncio.sleep(settings.pr_watch_seconds)
        try:
            await asyncio.to_thread(prs.watch_once)
        except Exception as exc:
            print(f"pr watch failed: {exc}")


async def _stream_traffic_forever() -> None:
    """What the moving dots are drawn from: one snapshot per project per second, only while there is traffic."""
    from . import perf
    tick, suggested = 0, {}
    while True:
        await asyncio.sleep(1.0)
        tick += 1
        for repo in db.select("repos"):
            snap = traffic.snapshot(repo["id"], window=12)
            if snap["nodes"]:
                sim = traffic.simulator(repo["id"])
                snap["simulation"] = {"profile": sim.profile, "target": sim.target} if sim else None
                if tick % 4 == 0:  # the standing recommendation: where adding a node would help right now
                    try:
                        # judged on 30 s, not on the 12 s the dots are drawn from: a suggestion should not flicker with every burst
                        suggested[repo["id"]] = await asyncio.to_thread(perf.suggestions, repo, traffic.snapshot(repo["id"], window=30))
                    except Exception as exc:
                        print(f"suggestions failed: {exc}")
                snap["suggestions"] = suggested.get(repo["id"], [])
                ui_events.broadcast({"t": "traffic", "repoId": repo["id"], "traffic": snap})


async def _audit_forever() -> None:
    """Each project's agent judges its whole pipeline on a timer. Silent: it writes the audit log, not the UI."""
    while True:
        await asyncio.sleep(settings.audit_seconds)
        try:
            await asyncio.to_thread(audit.audit_all)
        except Exception as exc:
            print(f"audit failed: {exc}")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.connect()
    events.bind_loop(asyncio.get_running_loop())
    ui_events.bind_loop(asyncio.get_running_loop())
    tasks = []
    if settings.poll_interval_seconds > 0:
        tasks.append(asyncio.create_task(_poll_forever()))
    if settings.env_watch_seconds > 0:
        tasks.append(asyncio.create_task(_watch_env_forever()))
    tasks.append(asyncio.create_task(_stream_traffic_forever()))
    if settings.audit_seconds > 0:
        tasks.append(asyncio.create_task(_audit_forever()))
    if settings.pr_watch_seconds > 0:
        tasks.append(asyncio.create_task(_watch_prs_forever()))
    yield
    for task in tasks:
        task.cancel()
    db.flush()  # everything queued reaches the database before the process exits


app = FastAPI(title="Chowkidaar API", version="0.1.0", lifespan=lifespan,
              description="Autonomous API maintenance: detects API contract changes, maps the affected code, generates and validates the migration, opens the pull request.")
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"])
app.include_router(router)
