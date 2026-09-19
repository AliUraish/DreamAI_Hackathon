"""Connecting repositories, building the integration map, and checking for drift."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path
from typing import Any

import networkx as nx

from . import agents, db, envwatch, events, gitops, notify, poller, ui_events
from .graph import affected_files, build_code_graph
from .providers import get_provider
from .scanner import ProviderUsage, scan_repo
from .validate import detect_commands

OPEN_MIGRATION_STATES = {"queued", "running", "pr_opened", "ready_local", "needs_review"}

_analysis_cache: dict[str, tuple[dict[str, ProviderUsage], nx.Graph]] = {}
_analysis_locks: dict[str, threading.Lock] = {}
_analysis_guard = threading.Lock()


def analysis(repo: dict[str, Any], *, refresh: bool = False) -> tuple[dict[str, ProviderUsage], nx.Graph]:
    """(scanner usages, Graphify code graph) for a repo, cached in memory.

    One build per repository at a time: after a restart the UI asks for the graph, the dashboard and an
    explanation at once, and concurrent builds used to collide on Graphify's output directory (a 500).
    A cold cache reuses the graph already on disk; only `refresh` rebuilds it."""
    with _analysis_guard:
        lock = _analysis_locks.setdefault(repo["id"], threading.Lock())
    with lock:
        if refresh or repo["id"] not in _analysis_cache:
            root = Path(repo["local_path"])
            _analysis_cache[repo["id"]] = (scan_repo(root), build_code_graph(root, repo["id"], force=refresh))
        return _analysis_cache[repo["id"]]


def _version_of(path: str | None) -> str | None:
    match = re.search(r"/(v\d+)(?:/|$)", path or "")
    return match.group(1) if match else None


def connect_repo(*, local_path: str | None = None, full_name: str | None = None, connected_via: str = "manual",
                 env: list[dict[str, Any]] | None = None, background: bool = False) -> dict[str, Any]:
    """Register a repository and map its API pipelines.

    `env` is a snapshot pushed by the connector (fingerprints computed on the user's machine).
    With `background=True` the mapping runs as the repository agent's first task and this returns at once."""
    if local_path:
        root = Path(local_path).expanduser().resolve()
        if not (root / ".git").exists():
            if not full_name:
                raise ValueError(f"{root} is not a git repository")
            root = gitops.clone_from_github(full_name)  # the connector ran on another machine: work from a clone
    elif full_name:
        root = gitops.clone_from_github(full_name)
    else:
        raise ValueError("give local_path or full_name")

    existing = next((r for r in db.select("repos") if r["local_path"] == str(root)), None)
    info = gitops.describe_repo(root)
    repo = existing or db.insert("repos", {
        "id": db.new_id("repo"), "name": (info["full_name"] or full_name or root.name).split("/")[-1], "full_name": info["full_name"] or full_name,
        "local_path": str(root), "remote_url": info["remote_url"], "default_branch": info["default_branch"],
        "validation_commands": detect_commands(root), "connected_via": connected_via, "env_source": "connector" if env is not None else "disk",
        "created_at": db.now()})
    if existing and env is not None and existing.get("env_source") != "connector":
        db.update("repos", repo["id"], {"env_source": "connector", "connected_via": connected_via})
        repo = db.get("repos", repo["id"])
    events.emit("repo.connected", f"Repository {repo['name']} connected", repo_id=repo["id"])
    if background:
        agents.submit(repo["id"], "Mapping the API pipelines", lambda: map_repo(repo, env))
    else:
        map_repo(repo, env)
    return repo


def map_repo(repo: dict[str, Any], env: list[dict[str, Any]] | None = None) -> None:
    """Build the repository's context step by step; each step is announced so the UI can grow the picture with it."""
    def phase(name: str, message: str, **extra: Any) -> None:
        agents.progress(repo["id"], message)
        ui_events.broadcast({"t": "map", "repoId": repo["id"], "phase": name, "msg": message, **extra})

    phase("env", "Reading environment variables")
    envwatch.check_repo(repo, env)  # first call records the baseline
    snapshot = db.get("env_snapshots", repo["id"])
    tracked = envwatch.public_view(snapshot["entries"]) if snapshot else []
    phase("env.done", f"{len([v for v in tracked if v['has_value']])} credential fingerprints saved, no values stored", variables=len(tracked))

    phase("scan", "Finding API call sites and building the code graph")
    integrations = sync_integrations(repo)
    usages, graph = analysis(repo)
    phase("pipelines", f"{len(integrations)} API pipeline(s) traced through {graph.number_of_nodes()} graph nodes", integrations=len(integrations))

    lines = []
    for integration in integrations:
        usage = usages.get(integration["provider"])
        sites = ", ".join(f"{c.function or c.file} ({c.method} {c.path})" for c in usage.call_sites if not c.is_test) if usage else "no call sites"
        lines.append(f"{integration['name']} {integration.get('version') or ''}: {sites}; files: {', '.join(f['path'] for f in integration['files'])}")
    checks = ", ".join(c["cmd"] for c in repo["validation_commands"] or []) or "none found"
    agents.remember(repo["id"], "pipeline", f"Repository {repo['name']} (branch {repo['default_branch']}). Checks: {checks}. Pipelines: " + " | ".join(lines))
    phase("done", "Pipeline context ready")


def sync_integrations(repo: dict[str, Any]) -> list[dict[str, Any]]:
    """Scan the repo and create/update one integration per provider found."""
    usages, graph = analysis(repo, refresh=True)
    current = {i["provider"]: i for i in db.select("integrations", {"repo_id": repo["id"]})}
    for provider_id, usage in usages.items():
        provider = get_provider(provider_id) or {}
        files = affected_files(graph, usage)
        versions = [v for v in (_version_of(c.path) for c in usage.call_sites if not c.is_test) if v]
        if provider_id in current:
            db.update("integrations", current[provider_id]["id"], {"files": files})
            continue
        endpoints = [{"id": p["id"], "provider": provider_id, "method": p["method"], "path": p["path"],
                      "url": f"{provider['base_url']}{p['path']}"} for p in provider.get("probes", [])]
        integration = db.insert("integrations", {
            "id": db.new_id("int"), "repo_id": repo["id"], "provider": provider_id, "name": usage.name,
            "version": versions[0] if versions else None, "status": "healthy" if endpoints else "unmonitored",
            "docs_url": provider.get("docs_url"), "endpoints": endpoints, "baseline": {}, "files": files,
            "last_checked_at": None, "created_at": db.now()})
        events.emit("integration.mapped", f"{usage.name}: {len(files)} files depend on this API",
                    repo_id=repo["id"], integration_id=integration["id"], data={"files": [f["path"] for f in files]})
        if endpoints:
            check_integration(integration["id"], announce=False)  # records the baseline
    return db.select("integrations", {"repo_id": repo["id"]}, order="name ASC")


def _fingerprint(changes: list[dict[str, Any]]) -> str:
    key = sorted((c["kind"], c["path"], str(c.get("after"))) for c in changes)
    return hashlib.sha256(json.dumps(key).encode()).hexdigest()[:12]


def check_integration(integration_id: str, *, announce: bool = True, trigger: str = "poll",
                      successor: dict[str, str] | None = None, docs_url: str | None = None,
                      to_version: str | None = None) -> dict[str, Any]:
    """Probe every endpoint of an integration. Returns {status, changes, migration_id}.

    A migration is created (but not started) when breaking drift is found; the
    caller schedules `pipeline.run_migration`.
    """
    integration = db.get("integrations", integration_id)
    if integration is None:
        raise KeyError(integration_id)
    provider = get_provider(integration["provider"]) or {}
    strict = integration["provider"] != "acme-orders"  # third-party list endpoints: optionality follows the data
    baseline = dict(integration["baseline"] or {})
    changes: list[dict[str, Any]] = []
    skipped, found_docs, successors = [], docs_url, {}

    for endpoint in integration["endpoints"]:
        hint = (successor or {}).get(endpoint["path"])
        successor_url = f"{provider.get('base_url', '')}{hint}" if hint and hint.startswith("/") else hint
        result = poller.check_endpoint(endpoint, baseline.get(endpoint["id"]), strict=strict, successor_url=successor_url)
        if result.get("skipped"):
            skipped.append(f"{endpoint['id']}: {result['skipped']}")
            continue
        if result["baseline"] and not result["changes"]:
            baseline[endpoint["id"]] = result["baseline"]
        changes += result["changes"] + result["renames"]
        found_docs = found_docs or result.get("docs_url")
        if result.get("successor_url") and result["successor_url"] != endpoint["url"]:
            successors[endpoint["id"]] = result["successor_url"]

    breaking = [c for c in changes if c["severity"] == "BREAKING"]
    deprecated = any(c["kind"] == "endpoint-deprecated" for c in changes)
    update: dict[str, Any] = {"baseline": baseline, "last_checked_at": db.now()}
    migration_id, created = None, False

    if skipped and len(skipped) == len(integration["endpoints"]):
        update["status"] = "unmonitored"
    elif breaking:
        open_migration = next((m for m in db.select("migrations", {"integration_id": integration_id})
                               if m["status"] in OPEN_MIGRATION_STATES), None)
        if open_migration:
            migration_id = open_migration["id"]
        else:
            new_paths = [c["after"] for c in changes if c["kind"] == "endpoint-changed"]
            from_version = integration["version"]
            to_version = to_version or next((v for v in map(_version_of, new_paths) if v), None)
            title = (f"Migrate {integration['name']} {from_version} to {to_version}" if from_version and to_version
                     else f"Update {integration['name']} integration for API contract drift")
            migration = db.insert("migrations", {
                "id": db.new_id("mig"), "repo_id": integration["repo_id"], "integration_id": integration_id, "title": title,
                "status": "queued", "kind": None, "trigger": trigger, "from_version": from_version, "to_version": to_version,
                "changes": changes, "affected_files": [], "steps": [], "validation": {}, "patched_files": [],
                "meta": {"fingerprint": _fingerprint(changes), "docs_url": found_docs, "successors": successors},
                "created_at": db.now(), "updated_at": db.now()})
            migration_id, created = migration["id"], True
            update["status"] = "breaking"
            events.emit("drift.detected", f"API change detected: {integration['name']}"
                        + (f" {from_version} -> {to_version}" if to_version else ""),
                        repo_id=integration["repo_id"], integration_id=integration_id, migration_id=migration_id,
                        data={"trigger": trigger, "breaking": len(breaking)})
            notify.send(f"{integration['name']} changed" + (f": {from_version} → {to_version}" if to_version else ""),
                        f"{len(breaking)} breaking change(s). The agent is on it.", repo_id=integration["repo_id"], level="warning")
    elif deprecated and integration["status"] == "healthy":
        update["status"] = "deprecated"
    elif announce and not changes and integration["status"] in {"healthy", "unmonitored"}:
        update["status"] = "healthy"

    db.update("integrations", integration_id, update)
    if announce and not breaking:
        events.emit("check.completed", f"{integration['name']}: no drift" if not changes else f"{integration['name']}: {len(changes)} non-breaking change(s)",
                    repo_id=integration["repo_id"], integration_id=integration_id, data={"skipped": skipped})
    return {"integration_id": integration_id, "status": update.get("status", integration["status"]), "changes": changes,
            "skipped": skipped, "migration_id": migration_id, "created": created}


def confirm_env_change(change_id: str, *, to_provider: str | None = None) -> dict[str, Any]:
    """The user said yes. Creates the migration (not started); the caller schedules the pipeline.

    Returns {env_change, migration_id}. migration_id is None when no code uses the old provider
    any more - then there is nothing to migrate and the integration map is simply refreshed."""
    change = db.get("env_changes", change_id)
    if change is None:
        raise KeyError(change_id)
    if change["status"] != "pending":
        raise ValueError(f"this change is already {change['status']}")
    to_provider = to_provider or change["to_provider"]
    if change["kind"] == "provider-switched" and not get_provider(to_provider or ""):
        raise ValueError("the new provider could not be recognised; pass to_provider")
    change["to_provider"] = to_provider
    repo = db.get("repos", change["repo_id"])
    details = change["details"]

    usages, _ = analysis(repo, refresh=True)
    usage = envwatch.usage_for_change(Path(repo["local_path"]), usages, change)
    old_provider, new_provider = get_provider(change["from_provider"] or "") or {}, get_provider(to_provider or "") or {}
    events.emit("env.change.confirmed", f"Confirmed: {change['summary']}", repo_id=repo["id"], data={"env_change_id": change_id})

    migration_id = None
    if usage.files:
        integration = next((i for i in db.select("integrations", {"repo_id": repo["id"]}) if i["provider"] == usage.provider), None)
        if integration is None:  # the old provider was only visible through its env var, not through a known call pattern
            integration = db.insert("integrations", {
                "id": db.new_id("int"), "repo_id": repo["id"], "provider": usage.provider, "name": usage.name, "version": None,
                "status": "breaking", "docs_url": old_provider.get("docs_url"), "endpoints": [], "baseline": {},
                "files": [], "last_checked_at": None, "created_at": db.now()})
        old_label = envwatch._label(change["from_provider"], details.get("from_platform"))
        new_label = envwatch._label(to_provider, details.get("to_platform"))
        changes = [{"kind": "env-renamed", "path": ".env", "severity": "BREAKING", "before": details["from_env"], "after": details["to_env"]}]
        title = f"Read {details['to_env']} instead of {details['from_env']}"
        if change["kind"] == "provider-switched":
            changes.insert(0, {"kind": "provider-switched", "path": "provider", "severity": "BREAKING", "before": old_label, "after": new_label})
            category = {"llm": "LLM", "vcs": "VCS"}.get(new_provider.get("category", ""), new_provider.get("category", "API"))
            title = f"Switch {category} provider: {old_label} to {new_label}"
        migration = db.insert("migrations", {
            "id": db.new_id("mig"), "repo_id": repo["id"], "integration_id": integration["id"], "title": title, "status": "queued",
            "kind": None, "trigger": "env-change", "from_version": old_label, "to_version": new_label, "changes": changes,
            "affected_files": [], "steps": [], "validation": {}, "patched_files": [],
            "meta": {"env_change_id": change_id, "env_change": {**change, "to_provider": to_provider},
                     "docs_url": new_provider.get("api_docs_url"), "fingerprint": _fingerprint(changes)},
            "created_at": db.now(), "updated_at": db.now()})
        migration_id = migration["id"]
        db.update("integrations", integration["id"], {"status": "breaking"})

    if to_provider and not any(i["provider"] == to_provider for i in db.select("integrations", {"repo_id": repo["id"]})):
        db.insert("integrations", {  # show the new provider straight away; its files appear once the code has moved
            "id": db.new_id("int"), "repo_id": repo["id"], "provider": to_provider, "name": new_provider.get("name", to_provider),
            "version": None, "status": "pending_code", "docs_url": new_provider.get("docs_url"), "endpoints": [], "baseline": {},
            "files": [], "last_checked_at": None, "created_at": db.now()})

    db.update("env_changes", change_id, {"status": "confirmed", "to_provider": to_provider, "migration_id": migration_id, "resolved_at": db.now()})
    agents.remember(repo["id"], "decision", f"The user confirmed: {change['summary']}")
    return {"env_change": db.get("env_changes", change_id), "migration_id": migration_id}


def dismiss_env_change(change_id: str) -> dict[str, Any]:
    change = db.get("env_changes", change_id)
    if change is None:
        raise KeyError(change_id)
    if change["status"] != "pending":
        raise ValueError(f"this change is already {change['status']}")
    db.update("env_changes", change_id, {"status": "dismissed", "resolved_at": db.now()})
    agents.remember(change["repo_id"], "decision", f"The user dismissed: {change['summary']} (it is not a provider switch; leave that code alone)")
    agents.set_state(change["repo_id"], "idle")
    events.emit("env.change.dismissed", f"Dismissed: {change['summary']}", repo_id=change["repo_id"], data={"env_change_id": change_id})
    return db.get("env_changes", change_id)


def start_migration(migration_id: str) -> None:
    """Hand a queued migration to its repository's agent."""
    from .pipeline import run_migration
    migration = db.get("migrations", migration_id)
    if migration:
        agents.submit(migration["repo_id"], migration["title"], lambda: run_migration(migration_id), migration_id=migration_id)


def question_event(change: dict[str, Any]) -> dict[str, Any]:
    question = envwatch.question_for(change)["question"]
    return {"t": "confirm.request", "repoId": change["repo_id"], "msg": f"Needs your confirmation · {change['summary']}", "request": {
        "id": change["id"], "repoId": change["repo_id"], "kind": change["kind"], "title": question["title"], "body": question["body"],
        "fromProviderId": f"provider:{change['from_provider']}" if change["from_provider"] else None,
        "toProviderId": f"provider:{change['to_provider']}" if change["to_provider"] else None,
        "needsProviderChoice": question["needs_provider_choice"], "providerOptions": question["provider_options"]}}


def check_env(repo: dict[str, Any], pushed: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Sense environment changes and apply the policy: act when it is safe, otherwise ask and wait."""
    created = envwatch.check_repo(repo, pushed)
    for note in envwatch.pop_notes(repo["id"]):
        if note["kind"] == "provider-added":
            recommend_route(repo, note["to_provider"], note["details"].get("to_env"))
    for change in created:
        if change["details"].get("automatic"):
            result = confirm_env_change(change["id"])
            notify.send("Environment variable renamed", f"{change['summary']}. Same credential, so the agent is updating the references.", repo_id=repo["id"])
            if result["migration_id"]:
                start_migration(result["migration_id"])
        else:
            ui_events.broadcast(question_event(change))
            agents.set_state(repo["id"], "waiting", f"Waiting for your answer: {change['summary']}")
            agents.remember(repo["id"], "decision", f"Asked the user: {change['summary']}")
            notify.send("Your decision is needed", envwatch.question_for(change)["question"]["title"], repo_id=repo["id"], level="action")
    return created


def recommend_route(repo: dict[str, Any], added_provider: str, env_name: str | None) -> dict[str, Any] | None:
    """Credentials for another provider of a kind this project already calls have appeared. Work out, on the traffic
    seen so far, whether sending part of the load there is better, and tell the user. Nothing is changed until they say so."""
    from . import perf, traffic
    added = get_provider(added_provider) or {}
    usages, graph = analysis(repo)
    rivals = [u for pid, u in usages.items() if pid != added_provider and (get_provider(pid) or {}).get("category") == added.get("category")]
    if not rivals:
        return None
    snap = traffic.snapshot(repo["id"], window=120)
    from .graph import _find_nodes
    sites = [(symbol, usage) for usage in rivals for symbol in _find_nodes(graph, usage)[1]]
    if not sites:
        return None
    node, usage = max(sites, key=lambda item: snap["nodes"].get(item[0], {}).get("load", 0.0))
    label = str(graph.nodes[node].get("label", node))
    stats, callers = snap["nodes"].get(node), perf.callers_of(snap, node)
    recommendation: dict[str, Any] = {"kind": "route", "nodeId": node, "label": label, "provider": added_provider, "providerName": added.get("name"),
                                      "env": env_name, "prefer": f"route:{added_provider}"}
    if stats and callers:
        sim = traffic.simulator(repo["id"])
        service_by_caller = {c["node"]: (sim.slot_ms(c["node"], node) if sim and snap["source"] == "simulated" else c["mean_ms"]) for c in callers}
        labels = {n: str(graph.nodes[n].get("label", n)) for n in graph.nodes}
        before, options = perf.build_options(node, label, stats, callers, service_by_caller, perf.alternative_providers(repo["id"], usage.provider), labels)
        option = next((o for o in options if o["id"] == f"route:{added_provider}"), None)
        if option:
            recommendation |= {"title": option["title"], "summary": option["summary"], "before": before, "after": option["after"], "gain": option["gain"], "source": snap["source"]}
    recommendation.setdefault("title", f"Route part of {label} to {added.get('name')}")
    recommendation.setdefault("summary", f"{added.get('name')} credentials are now configured ({env_name}). {label} is the only path to {usage.name}; "
                                         "there is no traffic yet to size the gain, so this is a suggestion, not a measurement.")
    body = recommendation["summary"] + (f" Simulated p95 {recommendation['before']['p95_ms'] / 1000:.1f} s → {recommendation['after']['p95_ms'] / 1000:.1f} s."
                                        if "after" in recommendation else "")
    agents.remember(repo["id"], "decision", f"Recommended: {recommendation['title']} (after {env_name} was added).")
    events.emit("route.recommended", f"Better route available: {recommendation['title']}", repo_id=repo["id"], data=recommendation)
    note = notify.send(f"Better route available: {recommendation['title']}", body, repo_id=repo["id"], level="action", data=recommendation)
    ui_events.broadcast({"t": "recommend", "repoId": repo["id"], "recommendation": recommendation | {"id": note["id"]}})
    return recommendation
