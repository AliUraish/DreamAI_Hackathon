from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from .. import agents, audit, db, envwatch, events, explain as explainer, llm, notify, perf, service, simrun, traffic, ui_events, workspace
from ..config import BACKEND_ROOT, settings
from ..graph import affected_from_usage, node_link_slice, pipeline_graph
from ..providers import all_providers

router = APIRouter(prefix="/api")


def _or_404(row: dict[str, Any] | None, what: str) -> dict[str, Any]:
    if row is None:
        raise HTTPException(status_code=404, detail=f"{what} not found")
    return row


def _latest_migration(integration_id: str) -> dict[str, Any] | None:
    rows = db.select("migrations", {"integration_id": integration_id}, limit=1)
    return rows[0] if rows else None


def _migration_card(m: dict[str, Any] | None) -> dict[str, Any] | None:
    if m is None:
        return None
    return {k: m.get(k) for k in ("id", "repo_id", "title", "status", "kind", "trigger", "from_version", "to_version", "pr_url", "pr_number", "pr_state",
                                  "merged_at", "branch", "created_at", "updated_at")} | {
        "review_rounds": len((m.get("review") or {}).get("rounds", [])),
        "breaking_changes": len([c for c in m["changes"] if c["severity"] == "BREAKING"]),
        "affected_files": len([f for f in m["affected_files"] if not f.get("read_only")]),
    }


def _integration_card(i: dict[str, Any]) -> dict[str, Any]:
    files = [f for f in (i["files"] or [])]
    return {"id": i["id"], "repo_id": i["repo_id"], "provider": i["provider"], "name": i["name"], "version": i["version"],
            "status": i["status"], "monitored": bool(i["endpoints"]), "last_checked_at": i["last_checked_at"],
            "files_affected": len(files), "files": files, "endpoints": i["endpoints"], "docs_url": i["docs_url"],
            "migration": _migration_card(_latest_migration(i["id"]))}


# --- meta -------------------------------------------------------------------


@router.get("/health")
def health():
    return {"ok": True}


@router.get("/providers")
def providers():
    return [{"id": p["id"], "name": p["name"], "monitored": bool(p["probes"]), "docs_url": p["docs_url"]} for p in all_providers()]


@router.get("/dashboard")
def dashboard():
    """Everything the main view needs in one call."""
    repos = []
    for repo in db.select("repos", order="created_at ASC"):
        integrations = [_integration_card(i) for i in db.select("integrations", {"repo_id": repo["id"]}, order="name ASC")]
        open_issues = len([i for i in integrations if i["status"] in {"breaking", "migrating", "needs_review", "deprecated"}])
        repos.append({"id": repo["id"], "name": repo["name"], "full_name": repo["full_name"], "default_branch": repo["default_branch"],
                      "connected_via": repo.get("connected_via"), "env_source": repo.get("env_source"), "open_issues": open_issues,
                      "integrations": integrations})
    pending = [service.question_event(c)["request"] for c in db.select("env_changes", {"status": "pending"}, order="created_at ASC")]
    return {"workspace": workspace.public(workspace.current()), "repos": repos, "pending": pending, "agents": agents.list_agents(),
            "unread": len([n for n in notify.recent(100) if not n["read"]]),
            "system": {"database": db.dialect(), "llm": llm.describe(), "github": bool(__import__("app.gitops", fromlist=["x"]).github_token())}}


# --- repositories -----------------------------------------------------------


class ConnectRepo(BaseModel):
    local_path: str | None = None
    full_name: str | None = None  # "owner/repo" on GitHub
    background: bool = False      # true: return at once and let the repository's agent map it (what the UI does)


@router.post("/repos", status_code=201)
def connect_repo(body: ConnectRepo):
    try:
        repo = service.connect_repo(local_path=body.local_path, full_name=body.full_name, background=body.background)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return repo | {"integrations": [_integration_card(i) for i in db.select("integrations", {"repo_id": repo["id"]}, order="name ASC")]}


@router.get("/repos")
def list_repos():
    return db.select("repos", order="created_at ASC")


@router.get("/repos/{repo_id}")
def get_repo(repo_id: str):
    repo = _or_404(db.get("repos", repo_id), "repository")
    return repo | {"integrations": [_integration_card(i) for i in db.select("integrations", {"repo_id": repo_id}, order="name ASC")]}


@router.post("/repos/{repo_id}/rescan")
def rescan_repo(repo_id: str):
    repo = _or_404(db.get("repos", repo_id), "repository")
    return [_integration_card(i) for i in service.sync_integrations(repo)]


@router.delete("/repos/{repo_id}", status_code=204)
def disconnect_repo(repo_id: str):
    _or_404(db.get("repos", repo_id), "repository")
    traffic.stop_simulation(repo_id)
    for table in ("events", "migrations", "integrations", "env_changes", "env_snapshots", "agents", "agent_memory", "explanations", "notifications", "audits"):
        db.delete(table, {"repo_id": repo_id})
    db.delete("repos", {"id": repo_id})


# --- environment sensing ----------------------------------------------------


def check_env_and_announce(repo: dict[str, Any]) -> list[dict[str, Any]]:
    return service.check_env(repo)


@router.get("/repos/{repo_id}/env")
def repo_env(repo_id: str):
    """Credential-like environment variables of a repo: names, providers, short fingerprints. Never values."""
    repo = _or_404(db.get("repos", repo_id), "repository")
    snapshot = db.get("env_snapshots", repo_id)
    return {"repo_id": repo["id"], "taken_at": snapshot["taken_at"] if snapshot else None,
            "variables": envwatch.public_view(snapshot["entries"]) if snapshot else []}


@router.post("/repos/{repo_id}/env/check")
def check_repo_env(repo_id: str):
    """Re-read the env files now (the watcher does this every few seconds anyway)."""
    repo = _or_404(db.get("repos", repo_id), "repository")
    return [envwatch.question_for(c) for c in check_env_and_announce(repo)]


@router.get("/env-changes")
def list_env_changes(status: str | None = "pending", repo_id: str | None = None):
    """Questions waiting for the user. `status=pending` (default) is what the UI should prompt for."""
    where = {k: v for k, v in {"status": status, "repo_id": repo_id}.items() if v}
    return [envwatch.question_for(c) for c in db.select("env_changes", where or None)]


class ConfirmEnvChange(BaseModel):
    to_provider: str | None = None  # required only when the new provider could not be recognised


@router.post("/env-changes/{change_id}/confirm", status_code=202)
def confirm_env_change(change_id: str, background: BackgroundTasks, body: ConfirmEnvChange | None = None):
    try:
        result = service.confirm_env_change(change_id, to_provider=body.to_provider if body else None)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="environment change not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    ui_events.broadcast({"t": "confirm.resolved", "id": change_id, "decision": "confirmed", "repoId": result["env_change"]["repo_id"]})
    if result["migration_id"]:
        service.start_migration(result["migration_id"])
    return result


@router.post("/env-changes/{change_id}/dismiss")
def dismiss_env_change(change_id: str):
    try:
        change = service.dismiss_env_change(change_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="environment change not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    ui_events.broadcast({"t": "confirm.resolved", "id": change_id, "decision": "dismissed", "repoId": change["repo_id"]})
    return change


# --- integrations -----------------------------------------------------------


@router.get("/integrations/{integration_id}")
def get_integration(integration_id: str):
    integration = _or_404(db.get("integrations", integration_id), "integration")
    repo = db.get("repos", integration["repo_id"])
    usages, _ = service.analysis(repo)
    usage = usages.get(integration["provider"])
    return _integration_card(integration) | {
        "call_sites": usage.to_dict()["call_sites"] if usage else [],
        "baseline": {k: {"hash": v["hash"], "sampled_at": v["sampled_at"]} for k, v in (integration["baseline"] or {}).items()},
        "migrations": [_migration_card(m) for m in db.select("migrations", {"integration_id": integration_id})],
    }


@router.get("/integrations/{integration_id}/graph")
def integration_graph(integration_id: str, depth: int | None = Query(None, ge=1, le=6)):
    """The pipeline graph for ONE maintained API - never the whole codebase."""
    integration = _or_404(db.get("integrations", integration_id), "integration")
    repo = db.get("repos", integration["repo_id"])
    usages, graph = service.analysis(repo)
    usage = usages.get(integration["provider"])
    if usage is None:
        raise HTTPException(status_code=409, detail="this API is no longer referenced in the repository; rescan")
    migration = _latest_migration(integration_id)
    changed = {c["path"] for c in (migration or {}).get("changes", []) if c["kind"].startswith("endpoint-")}
    patched = set((migration or {}).get("patched_files") or [])
    return pipeline_graph(graph, usage, integration, depth=depth, changed_paths=changed, patched_files=patched)


@router.get("/graph")
def graph_node_link(repo_id: str | None = None, integration_id: str | None = None, depth: int | None = Query(None, ge=1, le=6)):
    """Graphify graph.json shape (NetworkX node-link) for the interactive UI - restricted to the
    maintained API pipelines. Providers are nodes with file_type "provider", joined to the
    functions that call them by links with relation "calls_api"."""
    repos = db.select("repos", order="created_at ASC")
    repo = db.get("repos", repo_id) if repo_id else (repos[0] if repos else None)
    if repo is None:
        return {"directed": True, "multigraph": False, "graph": {"scope": "api-pipeline"}, "nodes": [], "links": []}
    usages, graph = service.analysis(repo)
    slices = [(usages[i["provider"]], i) for i in db.select("integrations", {"repo_id": repo["id"]}, order="name ASC")
              if i["provider"] in usages and (integration_id is None or i["id"] == integration_id)]
    patched = {i["id"]: set((_latest_migration(i["id"]) or {}).get("patched_files") or []) for _, i in slices}
    result = node_link_slice(graph, slices, depth, patched)
    result["graph"].update(repo=repo["name"], repo_id=repo["id"])
    if settings.prefetch_explanations and depth is None and integration_id is None:
        explainer.prefetch_links(repo, graph, usages, _distances(usages, graph), [(l["source"], l["target"]) for l in result["links"]])
    return result


@router.post("/integrations/{integration_id}/check")
def check_integration(integration_id: str, background: BackgroundTasks):
    _or_404(db.get("integrations", integration_id), "integration")
    result = service.check_integration(integration_id, trigger="poll")
    if result["created"]:
        service.start_migration(result["migration_id"])
    return result


@router.post("/check-all")
def check_all(background: BackgroundTasks):
    results = []
    for integration in db.select("integrations"):
        if not integration["endpoints"]:
            continue
        result = service.check_integration(integration["id"], trigger="poll")
        if result["created"]:
            service.start_migration(result["migration_id"])
        results.append(result)
    return results


# --- migrations -------------------------------------------------------------


@router.get("/migrations")
def list_migrations(repo_id: str | None = None, integration_id: str | None = None):
    where = {k: v for k, v in {"repo_id": repo_id, "integration_id": integration_id}.items() if v}
    return [_migration_card(m) for m in db.select("migrations", where or None)]


@router.get("/migrations/{migration_id}")
def get_migration(migration_id: str):
    migration = _or_404(db.get("migrations", migration_id), "migration")
    from ..schema import summarize_change
    migration["changes"] = [c | {"summary": summarize_change(c)} for c in migration["changes"]]
    migration["events"] = [e for e in db.events_after(0, {"migration_id": migration_id}, limit=2000) if e["type"] != "ui"]
    migration["ui_events"] = ui_events.replay(migration_id)
    return migration


@router.post("/migrations/{migration_id}/retry", status_code=202)
def retry_migration(migration_id: str, background: BackgroundTasks):
    migration = _or_404(db.get("migrations", migration_id), "migration")
    if migration["status"] in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="migration is already running")
    db.update("migrations", migration_id, {"status": "queued", "error": None})
    service.start_migration(migration_id)
    return {"id": migration_id, "status": "queued"}


# --- activity feed ----------------------------------------------------------


def _sse(queue_module, *, event_name: str | None, initial: list[dict[str, Any]], request: Request) -> StreamingResponse:
    queue = queue_module.subscribe()
    prefix = f"event: {event_name}\n" if event_name else ""

    async def generate():
        try:
            yield ": connected\n\n"
            for event in initial:
                yield f"{prefix}data: {json.dumps(event)}\n\n"
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"{prefix}data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            queue_module.unsubscribe(queue)

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/events")
def list_events(request: Request, after: int = 0, repo_id: str | None = None, integration_id: str | None = None,
                migration_id: str | None = None, replay: str | None = None, limit: int = Query(200, le=1000)):
    """Two representations of the same run.

    `Accept: text/event-stream` (what `new EventSource("/api/events")` sends): the typed
    `PipelineEvent` stream the graph UI plays back, as unnamed SSE messages.
    `?replay=latest` or `?replay=<migration id>` first re-sends that run's events.

    Otherwise: the human-readable activity feed as JSON."""
    if "text/event-stream" in request.headers.get("accept", ""):
        initial: list[dict[str, Any]] = []
        if replay:
            latest = db.select("migrations", limit=1)
            target = replay if replay != "latest" else (latest[0]["id"] if latest else None)
            initial = ui_events.replay(target) if target else []
        return _sse(ui_events, event_name=None, initial=initial, request=request)
    where = {k: v for k, v in {"repo_id": repo_id, "integration_id": integration_id, "migration_id": migration_id}.items() if v}
    return [e for e in db.events_after(after, where, limit) if e["type"] != "ui"]


@router.get("/events/stream")
async def stream_events(request: Request):
    """Human-readable activity feed, live (SSE, event name `activity`)."""
    return _sse(events, event_name="activity", initial=[], request=request)


# --- onboarding + workspace ---------------------------------------------------------


class Onboarding(BaseModel):
    name: str = "My workspace"


@router.get("/workspace")
def get_workspace():
    return workspace.public(workspace.current())


@router.post("/onboarding", status_code=201)
def onboard(body: Onboarding):
    """Creates the workspace and its API key. The key is returned once and never again."""
    try:
        created, key = workspace.create(body.name)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return workspace.public(created) | {"api_key": key, "connect_command": workspace.connect_command(key)}


@router.post("/workspace/rotate-key")
def rotate_key():
    try:
        key = workspace.rotate()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return workspace.public(workspace.current()) | {"api_key": key, "connect_command": workspace.connect_command(key)}


# --- the connector API (what a project talks to; authenticated with the workspace key) ------------


def _require_key(authorization: str | None) -> None:
    key = (authorization or "").removeprefix("Bearer ").strip()
    if not workspace.verify(key):
        raise HTTPException(status_code=401, detail="invalid or missing API key")


@router.get("/v1/connector.py", response_class=PlainTextResponse)
def connector_script():
    script = (BACKEND_ROOT / "app" / "static" / "connector.py").read_text()
    return PlainTextResponse(script.replace("__BACKEND__", settings.public_url.rstrip("/")), media_type="text/x-python")


@router.get("/v1/connector/config")
def connector_config(authorization: str | None = Header(None)):
    _require_key(authorization)
    from ..providers import all_providers
    return {"key_prefixes": {p["id"]: p["key_prefixes"] for p in all_providers() if p.get("key_prefixes")},
            "fingerprint_salt": workspace.fingerprint_salt()}


class EnvEntry(BaseModel):
    name: str
    fingerprint: str | None = None
    files: list[str] = []
    hint: str | None = None


class ConnectProject(BaseModel):
    local_path: str | None = None
    remote_url: str | None = None
    branch: str | None = None
    env: list[EnvEntry] = []


@router.post("/v1/connect")
def connect_project(body: ConnectProject, authorization: str | None = Header(None)):
    """A project announces itself. Returns at once; the repository's agent maps the pipelines in the background."""
    _require_key(authorization)
    from ..gitops import parse_full_name
    try:
        repo = service.connect_repo(local_path=body.local_path, full_name=parse_full_name(body.remote_url), connected_via="connector",
                                    env=[e.model_dump() for e in body.env], background=True)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"repo_id": repo["id"], "name": repo["name"], "url": f"{settings.app_url.rstrip('/')}/?project={repo['id']}"}


class EnvPush(BaseModel):
    repo_id: str
    env: list[EnvEntry]


@router.post("/v1/env")
def push_env(body: EnvPush, authorization: str | None = Header(None)):
    _require_key(authorization)
    repo = _or_404(db.get("repos", body.repo_id), "repository")
    created = service.check_env(repo, [e.model_dump() for e in body.env])
    return {"changes": [c["summary"] + (" (handled automatically)" if c["details"].get("automatic") else " (waiting for you in Chowkidaar)") for c in created]}


# --- traffic ---------------------------------------------------------------------------------------


@router.get("/v1/chowkidaar-traffic.ts", response_class=PlainTextResponse)
def traffic_reporter():
    return PlainTextResponse((BACKEND_ROOT / "app" / "static" / "chowkidaar-traffic.ts").read_text(), media_type="text/plain")


class ReportedSpan(BaseModel):
    function: str
    file: str
    parent: str | None = None
    ms: float
    ok: bool = True
    ts: float | None = None


class ReportedTraffic(BaseModel):
    repo_id: str
    spans: list[ReportedSpan]


@router.post("/v1/traffic", status_code=202)
def report_traffic(body: ReportedTraffic, authorization: str | None = Header(None)):
    """Spans from the reporter running inside a connected project. Functions are matched to graph nodes by file and name."""
    _require_key(authorization)
    repo = _or_404(db.get("repos", body.repo_id), "repository")
    _, graph = service.analysis(repo)
    by_name: dict[tuple[str, str], str] = {}
    for node_id, data in graph.nodes(data=True):
        by_name[(str(data.get("source_file")), str(data.get("label", "")).removesuffix("()"))] = node_id
    find = lambda file, fn: next((n for (f, name), n in by_name.items() if name == fn and (f == file or f.endswith(file) or file.endswith(f))), None)  # noqa: E731
    spans = []
    for s in body.spans:
        node = find(s.file, s.function)
        if node:
            spans.append({"node": node, "parent": find(s.file, s.parent) if s.parent else None, "ms": s.ms, "ok": s.ok, "ts": s.ts})
    if spans:
        if traffic.simulator(repo["id"]):
            traffic.stop_simulation(repo["id"])  # measured traffic replaces simulated traffic; the two are never mixed
        traffic.record(repo["id"], spans, "live")
    return {"accepted": len(spans), "unmatched": len(body.spans) - len(spans)}


@router.get("/repos/{repo_id}/traffic")
def repo_traffic(repo_id: str, window: int = Query(30, ge=5, le=300)):
    _or_404(db.get("repos", repo_id), "repository")
    sim = traffic.simulator(repo_id)
    return traffic.snapshot(repo_id, window) | {"simulation": {"profile": sim.profile, "target": sim.target} if sim else None}


class Simulate(BaseModel):
    profile: str = "steady"   # steady | pressure | off
    target: str | None = None  # node id to put under pressure; defaults to the busiest API call site


@router.post("/repos/{repo_id}/traffic/simulate")
def simulate_traffic(repo_id: str, body: Simulate):
    """Generate traffic along the project's real call graph when the project is not deployed where Chowkidaar can see it."""
    repo = _or_404(db.get("repos", repo_id), "repository")
    if body.profile == "off":
        traffic.stop_simulation(repo_id)
        return {"profile": "off"}
    usages, graph = service.analysis(repo)
    from ..providers import all_providers
    sim = traffic.start_simulation(repo_id, graph, usages, {p["id"]: p for p in all_providers()}, body.profile, body.target)
    if body.profile == "pressure" and not sim.target:
        rate = sim.rates()
        sites = [n for n in sim.budgets if len([c for c, t in sim.calls.items() if n in t]) >= 2] or list(sim.budgets)
        sim.target = max(sites, key=lambda n: rate.get(n, 0.0) * sim.slot_ms(None, n)) if sites else None
    return {"profile": sim.profile, "target": sim.target, "entries": len(sim.entries)}


@router.post("/repos/{repo_id}/simulation", status_code=202)
def run_simulation(repo_id: str):
    """One button: simulate normal then peak load along the project's real call graph, measure every node, report what is
    healthy and what is weak, and recommend a change with simulated before/after numbers. Opens nothing by itself."""
    repo = _or_404(db.get("repos", repo_id), "repository")
    if not simrun.start(repo):
        raise HTTPException(status_code=409, detail="a simulation is already running for this project")
    return {"started": True}


@router.get("/repos/{repo_id}/simulation")
def last_simulation(repo_id: str):
    _or_404(db.get("repos", repo_id), "repository")
    rows = [a for a in db.select("audits", {"repo_id": repo_id}, limit=200) if a["verdict"] == "simulation"]
    return {"running": simrun.exploring(repo_id), "report": rows[0]["metrics"] if rows else None, "at": rows[0]["created_at"] if rows else None}


@router.get("/repos/{repo_id}/audits")
def list_audits(repo_id: str, limit: int = Query(30, le=200)):
    """The agent's periodic judgements of this project's pipeline. Written to the log, not to the UI."""
    _or_404(db.get("repos", repo_id), "repository")
    return db.select("audits", {"repo_id": repo_id}, limit=limit)


@router.post("/repos/{repo_id}/audit")
def audit_now(repo_id: str, window: int = Query(60, ge=10, le=300)):
    return audit.run_audit(_or_404(db.get("repos", repo_id), "repository"), window=window)


class StartReview(BaseModel):
    node_id: str
    prefer: str | None = None  # an option id to implement, e.g. "route:anthropic" from a recommendation


@router.post("/repos/{repo_id}/review", status_code=202)
def start_review(repo_id: str, body: StartReview):
    repo = _or_404(db.get("repos", repo_id), "repository")
    run_id = perf.open_review(repo, body.node_id, trigger="user", prefer=body.prefer)
    if run_id is None:
        raise HTTPException(status_code=409, detail="a review for this node is already open, or the node is not part of a pipeline")
    return {"migration_id": run_id}


# --- agents, notifications, explanations ----------------------------------------------------


@router.get("/agents")
def list_agents():
    return agents.list_agents()


@router.get("/repos/{repo_id}/agent")
def repo_agent(repo_id: str):
    _or_404(db.get("repos", repo_id), "repository")
    agent = next(a for a in agents.list_agents() if a["repoId"] == repo_id)
    return agent | {"memory": agents.memories(repo_id)}


@router.get("/notifications")
def list_notifications():
    return notify.recent()


class MarkRead(BaseModel):
    ids: list[str] | None = None


@router.post("/notifications/read")
def read_notifications(body: MarkRead | None = None):
    notify.mark_read(body.ids if body else None)
    return {"ok": True}


class Explain(BaseModel):
    repo_id: str
    node_id: str | None = None
    source: str | None = None
    target: str | None = None
    mode: str = "ai"  # "facts" answers at once from the graph; "ai" has the model word it


@router.post("/explain")
def explain(body: Explain):
    """Why is this node, or this connection, in the pipeline?"""
    repo = _or_404(db.get("repos", body.repo_id), "repository")
    if not body.node_id and not (body.source and body.target):
        raise HTTPException(status_code=422, detail="give node_id, or source and target")
    usages, graph = service.analysis(repo)
    distances: dict[str, int] = {}
    for usage in usages.values():
        for node_id, d in affected_from_usage(graph, usage).items():
            distances[node_id] = min(distances.get(node_id, 99), d)
    for node_id in [x for x in (body.node_id, body.source, body.target) if x and not x.startswith("provider:")]:
        if node_id not in graph:
            raise HTTPException(status_code=404, detail=f"{node_id} is not in this repository's graph")
    return explainer.explain(repo, graph, usages, distances, node_id=body.node_id, source=body.source, target=body.target, mode=body.mode)


def _distances(usages, graph) -> dict[str, int]:
    distances: dict[str, int] = {}
    for usage in usages.values():
        for node_id, d in affected_from_usage(graph, usage).items():
            distances[node_id] = min(distances.get(node_id, 99), d)
    return distances


@router.get("/repos/{repo_id}/explanations")
def explanations_status(repo_id: str):
    """How many of this project's connections already have a model-written explanation."""
    _or_404(db.get("repos", repo_id), "repository")
    written = len([e for e in db.select("explanations", {"repo_id": repo_id}, limit=5000) if e["source"] != "graph" and e["target"].startswith("link:")])
    return explainer.prefetch_status(repo_id) | {"written": written, "ai_available": bool(llm.provider())}


# --- demo controls (the Acme Orders mock provider) --------------------------------


def _acme(path: str, method: str = "GET", json_body: dict | None = None) -> dict[str, Any]:
    import httpx
    from ..config import settings
    try:
        response = httpx.request(method, f"{settings.acme_orders_url.rstrip('/')}{path}", json=json_body, timeout=10)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"the demo provider is not reachable at {settings.acme_orders_url}") from exc


@router.get("/demo/state")
def demo_state():
    """Whether the demo provider is up and which version it serves. `available: false` hides the demo controls in the UI."""
    try:
        return {"available": True, **_acme("/admin/state")}
    except HTTPException:
        return {"available": False}


class DemoRelease(BaseModel):
    announce: bool = True  # true: the provider calls our release webhook. false: it ships silently and the next poll has to notice.
    mode: str = "sunset"


@router.post("/demo/release")
def demo_release(body: DemoRelease, request: Request):
    notify = f"{str(request.base_url).rstrip('/')}/api/webhooks/provider-release" if body.announce else None
    return _acme("/admin/release", "POST", {"mode": body.mode, "notify_url": notify})


@router.post("/demo/connect", status_code=201)
def demo_connect():
    """Connect the bundled demo app. Its git repository is created under the data directory, not inside the product."""
    from .. import demo
    repo = service.connect_repo(local_path=str(demo.materialize()), background=True)
    return repo


@router.post("/demo/reset")
def demo_reset():
    """Provider back to v1, and the demo project re-connected so its baseline and status start clean."""
    _acme("/admin/reset", "POST")
    from .. import demo
    # Only the demo project is reset. Any other connected project, its history and its agent's memory are left alone.
    demo_path = str(demo.materialize())
    for repo in db.select("repos"):
        if repo["local_path"] == demo_path:
            disconnect_repo(repo["id"])
            service._analysis_cache.pop(repo["id"], None)
    return {"version": "v1", "repos": [service.connect_repo(local_path=demo_path)["id"]]}


# --- provider-initiated trigger ---------------------------------------------


class ProviderRelease(BaseModel):
    provider: str
    from_version: str | None = None
    to_version: str | None = None
    successor: dict[str, str] | None = None  # {"/v1/orders": "/v2/orders"}
    docs_url: str | None = None


@router.post("/webhooks/provider-release", status_code=202)
def provider_release(body: ProviderRelease, background: BackgroundTasks):
    """An API provider announces a release. Fans out to every connected repository that uses that API."""
    affected = [i for i in db.select("integrations") if i["provider"] == body.provider]
    events.emit("release.announced", f"API release announced: {body.provider} {body.from_version or ''} -> {body.to_version or ''}".strip(),
                data=body.model_dump())
    results = []
    for integration in affected:
        result = service.check_integration(integration["id"], trigger="provider-release", successor=body.successor,
                                           docs_url=body.docs_url, to_version=body.to_version)
        if result["created"]:
            service.start_migration(result["migration_id"])
        results.append(result)
    return {"provider": body.provider, "repositories_affected": len(affected), "results": results}
