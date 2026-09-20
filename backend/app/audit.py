"""The audit: every few minutes each project's agent looks at the whole pipeline and judges it.

It reads the traffic of the last window and what happened recently (its own activity log), decides
`healthy` / `watch` / `pressure`, and writes that down: a row in `audits` and a line in
`.data/audit/<repo>.jsonl`. A routine audit says nothing in the UI. Only a judgement that needs work
(a node under pressure) starts a review, and that is what the user sees.
"""

from __future__ import annotations

import json
from typing import Any

from . import db, perf, traffic
from .config import settings

WATCH_LOAD = 0.6
ERROR_RATE = 0.05


def _recent_review(repo_id: str, minutes: int = 30) -> bool:
    """The silent audit opens one pull request, then gives people time to look at it. Pressure on five call sites must not
    become five pull requests in twenty-five minutes. A person can still start any review from the UI."""
    from datetime import datetime, timedelta, timezone
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    for m in db.select("migrations", {"repo_id": repo_id}):
        if m.get("kind") == "performance" and m.get("trigger") == "audit":
            if m["status"] in {"queued", "running"} or datetime.fromisoformat(m["created_at"]) >= since:
                return True
    return False


def root_causes(snap: dict[str, Any], pressure: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Causes before symptoms: a function that is slow because something it calls, however far down, is past its budget is
    not the one to change."""
    pressured = {f["node"] for f in pressure}
    calls: dict[str, set[str]] = {}
    for key in snap.get("links", {}):
        a, b = key.split("->", 1)
        calls.setdefault(a, set()).add(b)

    def downstream(node: str) -> set[str]:
        seen, stack = set(), list(calls.get(node, ()))
        while stack:
            n = stack.pop()
            if n not in seen:
                seen.add(n)
                stack.extend(calls.get(n, ()))
        return seen - {node}

    return [f for f in pressure if not downstream(f["node"]) & pressured] or pressure


def run_audit(repo: dict[str, Any], *, window: int = 300, act: bool = True) -> dict[str, Any]:
    from . import service
    snap = traffic.snapshot(repo["id"], window=window)
    _, graph = service.analysis(repo)
    label = lambda n: str(graph.nodes[n].get("label", n)) if n in graph else n.removeprefix("provider:")  # noqa: E731
    findings: list[dict[str, Any]] = []
    for node, m in snap["nodes"].items():
        if node.startswith("provider:"):
            if m["errors"] >= ERROR_RATE:
                findings.append({"kind": "provider-errors", "node": node, "label": label(node), "detail": f"{m['errors']:.0%} of calls failed"})
            continue
        if m["load"] >= perf.PRESSURE_LOAD and m["calls"] >= 20:
            findings.append({"kind": "pressure", "node": node, "label": label(node),
                             "detail": f"load {m['load']:.2f} of a budget of {m['budget']} · p95 {m['p95_ms']:.0f} ms · {m['rps']:.1f} calls/s"})
        elif m["load"] >= WATCH_LOAD:
            findings.append({"kind": "watch", "node": node, "label": label(node), "detail": f"load {m['load']:.2f}, approaching its budget"})
        if m["errors"] >= ERROR_RATE:
            findings.append({"kind": "errors", "node": node, "label": label(node), "detail": f"{m['errors']:.0%} of calls failed"})

    recent = [e for e in db.events_after(0, {"repo_id": repo["id"]}, limit=4000)[-200:] if e["type"] != "ui"]
    failures = [e["message"] for e in recent if e["type"].endswith((".failed", "failed")) or "error" in (e["message"] or "").lower()][-5:]
    verdict = "pressure" if any(f["kind"] == "pressure" for f in findings) else "watch" if findings or failures else "healthy"
    busiest = sorted(((n, m) for n, m in snap["nodes"].items() if not n.startswith("provider:")), key=lambda item: -item[1]["load"])[:5]
    audit = {
        "id": db.new_id("aud"), "repo_id": repo["id"], "verdict": verdict, "source": snap["source"], "window_seconds": window,
        "summary": (f"{len(snap['nodes'])} nodes carried traffic ({snap['source']}). " if snap["nodes"] else "No traffic seen. ")
                   + (f"Busiest: {label(busiest[0][0])} at load {busiest[0][1]['load']:.2f}. " if busiest else "")
                   + (f"{len(findings)} finding(s). " if findings else "Nothing needs attention. ") + (f"Recent failures in the log: {len(failures)}." if failures else ""),
        "findings": findings, "metrics": {"busiest": [{"node": n, "label": label(n), **m} for n, m in busiest], "log_failures": failures},
        "action": None, "created_at": db.now()}

    from . import simrun
    if simrun.exploring(repo["id"]):
        audit["action"] = "none: a what-if simulation is driving the traffic"
    elif act and verdict == "pressure" and _recent_review(repo["id"]):
        audit["action"] = "none: a review this audit started is less than 30 minutes old; one pull request at a time"
    elif act and verdict == "pressure":
        causes = root_causes(snap, [f for f in findings if f["kind"] == "pressure"])
        waiting = []
        for hot in sorted(causes, key=lambda f: -snap["nodes"][f["node"]]["load"]):
            run_id = perf.open_review(repo, hot["node"], trigger="audit")
            if run_id:
                audit["action"] = f"started review {run_id} for {hot['label']}" + (f" (already open: {', '.join(waiting)})" if waiting else "")
                break
            waiting.append(hot["label"])
        else:
            audit["action"] = f"a review for {', '.join(waiting)} is already open"

    db.insert("audits", audit)
    log = settings.data_dir / "audit" / f"{repo['id']}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as handle:
        handle.write(json.dumps({k: audit[k] for k in ("created_at", "verdict", "source", "summary", "findings", "action")}) + "\n")
    return audit


def audit_all() -> list[dict[str, Any]]:
    return [run_audit(repo) for repo in db.select("repos", order="created_at ASC")]
