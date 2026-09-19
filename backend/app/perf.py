"""When a node is under pressure: measure it, work out how to relieve it, prove the gain in simulation,
write the change, run the project's checks, open the pull request.

The numbers in a review are never the model's opinion. Every option is run through the same queueing
model that the traffic simulator uses (`traffic.response`), on the rates and service times that were
observed. The LLM's job is to choose between the simulated options, explain why, and write the code.
"""

from __future__ import annotations

import re
import traceback
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from . import agents, db, events, gitops, llm, notify, traffic, ui_events
from .config import settings
from .graph import _find_nodes, affected_files, affected_from_usage, trace_hits
from .providers import all_providers, get_provider
from .repair import RepairUnavailable, generate_repair
from .scanner import ProviderUsage
from .validate import run_validation

PRESSURE_LOAD = 0.85


class Choice(BaseModel):
    option_id: str = Field(description="The id of the option to implement, exactly as given.")
    diagnosis: str = Field(description="Two sentences: what is wrong at this node and why it shows up as pressure now.")
    why: str = Field(description="Two or three sentences: why this option beats the others for this code, referring to the simulated numbers.")
    risks: list[str] = Field(description="What a reviewer should watch for if this is merged. Empty when none.")


# --- finding the pressure ------------------------------------------------------------------


def hottest(snapshot: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """The most loaded function (not a provider: we cannot change their code) over the threshold."""
    candidates = [(n, m) for n, m in snapshot["nodes"].items() if not n.startswith("provider:") and m["load"] >= PRESSURE_LOAD and m["calls"] >= 20]
    return max(candidates, key=lambda item: item[1]["load"]) if candidates else None


def callers_of(snapshot: dict[str, Any], node: str) -> list[dict[str, Any]]:
    found = []
    for key, link in snapshot["links"].items():
        source, _, target = key.partition("->")
        if target == node:
            found.append({"node": source, "rps": link["rps"], "mean_ms": link["mean_ms"]})
    return sorted(found, key=lambda c: -c["rps"])


# --- options, each with simulated numbers -------------------------------------------------------


def _mix(parts: list[tuple[float, dict[str, float]]]) -> dict[str, float]:
    total = sum(rate for rate, _ in parts) or 1.0
    saturated = any(r["saturated"] for _, r in parts)
    return {"p95_ms": round(sum(rate * r["p95_ms"] for rate, r in parts) / total, 1), "load": round(max(r["load"] for _, r in parts), 3),
            "capacity_rps": round(sum(r["capacity_rps"] for _, r in parts), 2), "saturated": saturated}


def build_options(node: str, label: str, stats: dict[str, Any], callers: list[dict[str, Any]], service_by_caller: dict[str, float],
                  alternatives: list[dict[str, Any]], labels: dict[str, str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(before, options). `service_by_caller` is the time one call holds a slot, without queueing, per caller."""
    budget = stats["budget"]
    rate = sum(c["rps"] for c in callers) or stats["rps"]
    service = sum(c["rps"] * service_by_caller[c["node"]] for c in callers) / rate if callers else stats["mean_ms"]
    before = traffic.response(rate, service, budget)
    options: list[dict[str, Any]] = []
    base = label.removesuffix("()")

    if len(callers) >= 2:
        slow = max(callers, key=lambda c: service_by_caller[c["node"]])
        parts, new_nodes = [], []
        for caller in callers:
            name = f"{base}For{labels[caller['node']].removesuffix('()')[:1].upper()}{labels[caller['node']].removesuffix('()')[1:]}()"
            new_nodes.append({"id": f"proposed:{name}", "label": name, "from": caller["node"]})
            parts.append((caller["rps"], traffic.response(caller["rps"], service_by_caller[caller["node"]], budget)))
        options.append({"id": "split", "kind": "split", "title": f"Split {label} per caller",
                        "summary": f"One call site per caller, each with its own budget of {budget} concurrent calls, so the slow {labels[slow['node']]} "
                                   f"traffic ({service_by_caller[slow['node']] / 1000:.1f} s per call) stops starving the fast callers.",
                        "new_nodes": new_nodes, "after": _mix(parts)})

    for alt in alternatives:
        light = min(callers, key=lambda c: service_by_caller[c["node"]]) if callers else None
        if not light:
            break
        rest = [c for c in callers if c is not light]
        rest_rate = sum(c["rps"] for c in rest)
        rest_service = sum(c["rps"] * service_by_caller[c["node"]] for c in rest) / rest_rate if rest_rate else service
        moved = traffic.response(light["rps"], alt["latency_ms"], budget)
        name = f"{base}Via{alt['name'].replace(' ', '')}()"
        options.append({"id": f"route:{alt['id']}", "kind": "route", "title": f"Route {labels[light['node']]} to {alt['name']}",
                        "summary": f"{alt['name']} credentials are configured ({alt['env']}). Sending the {labels[light['node']]} calls there takes "
                                   f"{light['rps']:.1f} of {rate:.1f} calls/s off {label}.",
                        "new_nodes": [{"id": f"proposed:{name}", "label": name, "from": light["node"], "provider": f"provider:{alt['id']}"}],
                        "after": _mix([(rest_rate, traffic.response(rest_rate, rest_service, budget)), (light["rps"], moved)])})

    limited = traffic.response(rate, min(service, service * 0.82), budget)  # a timeout trims the slow tail that holds slots longest
    options.append({"id": "limit", "kind": "limit", "title": f"Bound {label} with a timeout and a small queue",
                    "summary": "Keeps the single call site. A per-call timeout frees slots held by stuck requests and a bounded queue sheds load "
                               "instead of letting every request slow down.", "new_nodes": [], "after": {k: limited[k] for k in ("p95_ms", "load", "capacity_rps", "saturated")}})

    for option in options:
        after = option["after"]
        option["gain"] = {"p95_pct": round((1 - after["p95_ms"] / before["p95_ms"]) * 100) if before["p95_ms"] else 0,
                          "capacity_pct": round((after["capacity_rps"] / before["capacity_rps"] - 1) * 100) if before["capacity_rps"] else 0}
    options.sort(key=lambda o: (o["after"]["saturated"], o["after"]["p95_ms"]))
    return {**before, "rps": round(rate, 2), "service_ms": round(service, 1), "budget": budget}, options


SUGGEST_LOAD = 0.5


def suggestions(repo: dict[str, Any], snap: dict[str, Any]) -> list[dict[str, Any]]:
    """Looked at every few seconds while traffic flows: for each API call site past half of its budget, the best way to
    relieve it by ADDING a node (a call site per caller, or a route to another provider). Pure arithmetic on the snapshot:
    no LLM, nothing is changed. The UI draws the proposed node on the graph next to the busy one."""
    from . import service
    usages, graph = service.analysis(repo)
    sim = traffic.simulator(repo["id"])
    labels = {n: str(graph.nodes[n].get("label", n)) for n in graph.nodes}
    found = []
    for node, stats in snap["nodes"].items():
        if node.startswith("provider:") or node not in graph or stats["load"] < SUGGEST_LOAD or stats["budget"] > traffic.OUTBOUND_BUDGET:
            continue
        callers = callers_of(snap, node)
        usage = next((u for u in usages.values() if node in _find_nodes(graph, u)[1]), None)
        if not callers or not usage:
            continue
        slots = {c["node"]: (sim.slot_ms(c["node"], node) if sim and snap["source"] == "simulated" else c["mean_ms"]) for c in callers}
        before, options = build_options(node, labels[node], stats, callers, slots, alternative_providers(repo["id"], usage.provider), labels)
        adding = [o for o in options if o["new_nodes"] and o["after"]["p95_ms"] < before["p95_ms"]]
        if adding:
            best = adding[0]
            found.append({"nodeId": node, "label": labels[node], "prefer": best["id"], "kind": best["kind"], "title": best["title"], "summary": best["summary"],
                          "new_nodes": best["new_nodes"], "before": before, "after": best["after"], "gain": best["gain"]})
    return sorted(found, key=lambda s: -s["before"]["load"])[:3]


def alternative_providers(repo_id: str, provider_id: str) -> list[dict[str, Any]]:
    """Other providers of the same kind whose credentials this project already has."""
    category = (get_provider(provider_id) or {}).get("category")
    snapshot = db.get("env_snapshots", repo_id)
    if not category or not snapshot:
        return []
    configured = {e["provider"]: e["name"] for e in snapshot["entries"].values() if e.get("provider") and e.get("fingerprint")}
    return [{"id": p["id"], "name": p["name"], "env": configured[p["id"]], "latency_ms": traffic.PROVIDER_LATENCY_MS.get(category, 300) * 0.8}
            for p in all_providers() if p.get("category") == category and p["id"] != provider_id and p["id"] in configured]


# --- the run ---------------------------------------------------------------------------------------


def open_review(repo: dict[str, Any], node: str, *, trigger: str = "audit", prefer: str | None = None) -> str | None:
    """Create the run for a node under pressure and hand it to the repository's agent. One open run per node."""
    from . import service
    usages, graph = service.analysis(repo)
    if node not in graph:
        return None
    # One open run per node and per requested option: an audit does not reopen what is already in review, but the user
    # can still ask for a different route (a recommendation after a new provider key) while an earlier PR is open.
    for m in db.select("migrations", {"repo_id": repo["id"]}):
        meta = m.get("meta") or {}
        if meta.get("node") == node and meta.get("prefer") == prefer and m["status"] in {"queued", "running", "pr_opened", "ready_local"}:
            return None
    usage = next((u for u in usages.values() if node in affected_from_usage(graph, u)), None)
    integration = next((i for i in db.select("integrations", {"repo_id": repo["id"]}) if usage and i["provider"] == usage.provider), None)
    if not usage or not integration:
        return None
    label = graph.nodes[node].get("label", node)
    migration = db.insert("migrations", {
        "id": db.new_id("mig"), "repo_id": repo["id"], "integration_id": integration["id"], "title": f"Relieve pressure on {label}", "status": "queued",
        "kind": "performance", "trigger": trigger, "from_version": None, "to_version": None, "changes": [], "affected_files": [], "steps": [],
        "validation": {}, "patched_files": [], "meta": {"node": node, "label": label, "provider": usage.provider, "prefer": prefer},
        "created_at": db.now(), "updated_at": db.now()})
    agents.submit(repo["id"], migration["title"], lambda: run_review(migration["id"]), migration_id=migration["id"])
    return migration["id"]


def run_review(migration_id: str) -> None:
    migration = db.get("migrations", migration_id)
    try:
        _run(migration)
    except Exception as exc:
        traceback.print_exc()
        db.update("migrations", migration_id, {"status": "failed", "error": str(exc), "updated_at": db.now()})
        events.emit("review.failed", f"Pressure review failed: {exc}", repo_id=migration["repo_id"], migration_id=migration_id)
        ui_events.emit(migration_id, {"t": "done", "msg": f"Pressure review failed: {exc}"}, repo_id=migration["repo_id"])
    finally:
        gitops.discard(settings.work_dir / migration_id)


def _run(migration: dict[str, Any]) -> None:
    from . import service
    repo = db.get("repos", migration["repo_id"])
    integration = db.get("integrations", migration["integration_id"])
    meta, mid = migration["meta"], migration["id"]
    node, label = meta["node"], meta["label"]
    ids = {"repo_id": repo["id"], "integration_id": integration["id"], "migration_id": mid}
    ui = lambda t, msg=None, **payload: ui_events.emit(mid, {"t": t, "msg": msg, "kind": "pressure", **payload}, repo_id=repo["id"])  # noqa: E731
    save = lambda **values: db.update("migrations", mid, {**values, "updated_at": db.now()})  # noqa: E731
    save(status="running")

    usages, graph = service.analysis(repo)
    usage: ProviderUsage = usages[meta["provider"]]
    labels = {n: str(graph.nodes[n].get("label", n)) for n in graph.nodes}

    # 1. measure
    snap = traffic.snapshot(repo["id"], window=30)  # the same window the audit judged
    stats = snap["nodes"].get(node)
    if not stats:
        raise RuntimeError("no traffic has been seen on this node")
    callers = callers_of(snap, node)
    sim = traffic.simulator(repo["id"])
    # How long one call from each caller occupies a slot. Simulated traffic knows this exactly; for reported traffic the
    # observed mean per caller is used, which still contains some queueing and so understates the gain rather than inflating it.
    service_by_caller = {c["node"]: (sim.slot_ms(c["node"], node) if sim and snap["source"] == "simulated" else c["mean_ms"]) for c in callers}
    provider_node = f"provider:{meta['provider']}"
    ui("stage", stage="detect")
    ui("release", f"Pressure detected · {label}", providerId=provider_node, **{"from": f"load {stats['load']:.2f}", "to": f"p95 {stats['p95_ms'] / 1000:.1f} s"},
       source=f"{'simulated' if snap['source'] == 'simulated' else 'live'} traffic · {stats['rps']:.1f} calls/s against a budget of {stats['budget']} concurrent")
    ui("pressure", nodeId=node, metrics=stats, source=snap["source"])
    events.emit("pressure.detected", f"{label} is under pressure: load {stats['load']:.2f}, p95 {stats['p95_ms'] / 1000:.1f} s at {stats['rps']:.1f} calls/s", data=stats, **ids)
    notify.send(f"{label} is under pressure", f"Load {stats['load']:.2f} · p95 {stats['p95_ms'] / 1000:.1f} s. The agent is working out how to relieve it.",
                repo_id=repo["id"], level="warning")

    # 2. who sends the traffic
    ui("stage", "Measuring who sends the traffic", stage="diff")
    for caller in callers:
        share = caller["rps"] / (stats["rps"] or 1)
        ui("change", f"{labels[caller['node']]} sends {share:.0%} of the calls", change={
            "id": caller["node"], "kind": "semantic" if service_by_caller[caller["node"]] > 2000 else "request", "label": f"{share:.0%} of the traffic",
            "before": labels[caller["node"]], "after": f"{caller['rps']:.1f} calls/s · {service_by_caller[caller['node']] / 1000:.1f} s each",
            "where": f"calls {label}", "nodeIds": [caller["node"], node],
            "note": "Slow calls hold a slot for seconds; the fast caller waits behind them." if service_by_caller[caller["node"]] > 2000 else None})

    # 3. what depends on it
    ui("stage", f"Tracing what depends on {label}", stage="trace")
    hits = [h for h in trace_hits(graph, usage) if h["nodeId"] == node or any(c["node"] == h["nodeId"] for c in callers) or h["role"] in {"change", "test"}]
    for index, hit in enumerate(hits):
        ui("hit", f"{len(callers)} caller(s) share this call site" if index == len(hits) - 1 else None, hit=hit)

    # 4. options, simulated, then chosen
    ui("stage", "Simulating ways to relieve it", stage="docs")
    before, options = build_options(node, label, stats, callers, service_by_caller, alternative_providers(repo["id"], meta["provider"]), labels)
    choice = None
    if llm.provider():
        facts = "\n".join(f"- option `{o['id']}`: {o['title']}. {o['summary']} Simulated: p95 {before['p95_ms']:.0f} ms -> {o['after']['p95_ms']:.0f} ms, "
                          f"capacity {before['capacity_rps']} -> {o['after']['capacity_rps']} calls/s, load -> {o['after']['load']}." for o in options)
        source_file = graph.nodes[node].get("source_file")
        code = (Path(repo["local_path"]) / source_file).read_text(errors="ignore")[-9000:] if source_file else ""
        try:
            choice = llm.structured(
                "You are a performance reviewer for one function of a production service. You are given measured traffic, a set of options that were "
                "already simulated with a queueing model, and the code. Choose one option id from the list. Do not invent numbers: quote the simulated ones.",
                f"## Node under pressure\n{label} in {source_file}\nrate {before['rps']} calls/s, time per call {before['service_ms']} ms, concurrency budget "
                f"{before['budget']}, load {before['load']}, p95 {before['p95_ms']} ms, saturated: {before['saturated']}\n\n## Callers\n"
                + "\n".join(f"- {labels[c['node']]}: {c['rps']} calls/s, {service_by_caller[c['node']]:.0f} ms per call" for c in callers)
                + f"\n\n## Options (simulated)\n{facts}\n\n## Notes on this repository\n{agents.recall(repo['id']) or 'none'}\n\n## Code\n```\n{code}\n```", Choice, effort="low")
        except llm.LLMUnavailable:
            choice = None
    preferred = meta.get("prefer")
    chosen = next((o for o in options if o["id"] == preferred), None) or next((o for o in options if choice and o["id"] == choice.option_id), None) or options[0]
    review = {"nodeId": node, "label": label, "before": before, "options": options, "recommended": chosen["id"],
              "diagnosis": choice.diagnosis if choice else f"{label} is the only path to {usage.name} and runs at load {before['load']}: calls queue for a free slot.",
              "why": choice.why if choice else f"It has the lowest simulated p95 ({chosen['after']['p95_ms']:.0f} ms) of the options.",
              "risks": choice.risks if choice else [], "source": snap["source"], "decidedBy": llm.provider() if choice else "simulation"}
    ui("review", f"Recommended · {chosen['title']} · p95 {before['p95_ms'] / 1000:.1f} s → {chosen['after']['p95_ms'] / 1000:.1f} s (simulated)", review=review)
    save(meta={**meta, "review": review}, summary=review["why"])
    agents.remember(repo["id"], "decision", f"Pressure on {label}: recommended '{chosen['title']}' (simulated p95 {before['p95_ms']:.0f} -> {chosen['after']['p95_ms']:.0f} ms).")

    # 5. write it
    files = [f for f in affected_files(graph, usage) if f["depth"] <= 1]
    root = Path(repo["local_path"])
    for f in files:
        f["read_only"] = any(m in f"/{f['path']}" for m in ("/tests/", "/test/", ".test.", ".spec."))
        f["content"] = (root / f["path"]).read_text(encoding="utf-8", errors="ignore")
    file_node = {graph.nodes[n].get("source_file"): n for n in affected_from_usage(graph, usage)
                 if graph.nodes[n].get("source_location") == "L1" and not str(graph.nodes[n].get("label", "")).endswith("()")}
    save(affected_files=[{k: f[k] for k in ("path", "depth", "role", "read_only")} for f in files])
    target_file = graph.nodes[node].get("source_file")
    ui("stage", f"Writing the change in {target_file}", stage="patch")
    if target_file in file_node:
        ui("patch.start", nodeId=file_node[target_file])

    slug = re.sub(r"[^a-z0-9]+", "-", f"relieve-{label}-{meta.get('prefer') or ''}".lower()).strip("-")
    branch = f"chowkidaar/{slug}"
    if repo.get("full_name"):  # the pull request must contain our change only: start from what is on GitHub, not from the local checkout
        workdir = gitops.checkout_remote_branch(repo["full_name"], repo["default_branch"], settings.work_dir / mid, root)
        gitops.git(workdir, "checkout", "-b", branch)
    else:
        workdir = gitops.make_working_copy(root, mid, repo["default_branch"], branch)
    provider = get_provider(meta["provider"]) or {}
    commands = repo["validation_commands"] or []
    baseline = run_validation(workdir, commands, provider.get("validation_env", {})) if commands else {"passed": True, "checks": []}
    # A check that already fails on the untouched base says nothing about this change: report it, do not gate on it.
    gating = [c for c, result in zip(commands, baseline["checks"]) if result["passed"]]
    if commands and not gating:
        reason = "none of the project's checks pass on the untouched base branch, so a change could not be validated"
        ui("done", f"No change was opened: {reason}")
        save(status="needs_review", error=reason, validation={"before": baseline})
        notify.send("Pressure review needs you", f"{label}: {reason}", repo_id=repo["id"], level="action")
        return

    task = {"kind": "performance", "summary": f"{label} is under pressure. Implement: {chosen['title']}. {chosen['summary']}",
            "details": {"from_env": "-", "to_env": "-"}, "review": review, "option": chosen}
    result, after, previous, reason = None, None, None, None
    for attempt in range(1, settings.max_repair_attempts + 1):
        agents.progress(repo["id"], f"Writing the change (attempt {attempt})")
        try:
            result = generate_repair(integration=integration, changes=[], docs={"text": None, "error": "not applicable"}, files=files,
                                     previous_attempt=previous, memory=agents.recall(repo["id"]), perf=task)
        except RepairUnavailable as exc:
            reason = str(exc)
            break
        if result.outcome != "patched" or not result.files:
            reason = "the model judged the change unsafe to make automatically"
            break
        patched = gitops.write_files(workdir, [f.model_dump() for f in result.files])
        save(patched_files=patched)
        for patch in ui_events.build_patches(gitops.diff(workdir, patched), file_node):
            ui("patch.done", f"Patched {patch['path']} · +{patch['additions']} −{patch['deletions']}", patch=patch)
        if attempt == 1:
            ui("stage", "Running the project's checks and the traffic simulation", stage="verify")
            for check in ui_events.build_checks(baseline, "baseline", file_node):
                ui("check", check=check)
        for command in gating:  # shown running while they run, the way a terminal would
            ui("check", check={"id": command["name"], "group": {"typecheck": "types"}.get(command["name"], "build" if command["name"] != "tests" else "tests"),
                               "name": command["cmd"].replace(" --silent", ""), "cmd": command["cmd"].replace(" --silent", ""), "phase": "patched", "status": "running"})
        after = run_validation(workdir, gating, provider.get("validation_env", {})) if gating else {"passed": True, "checks": []}
        for check in ui_events.build_checks(after, "patched", file_node):
            ui("check", check=check)
        if after["passed"]:
            reason = None
            break
        previous = {"files": [f.model_dump() for f in result.files], "output": "\n\n".join(f"$ {c['cmd']}\n{c['output']}" for c in after["checks"] if not c["passed"])}
        reason = "the change did not pass the project's checks"

    if reason:
        ui("done", f"No change was opened: {reason}")
        save(status="needs_review", error=reason, validation={"before": baseline, "after": after})
        notify.send("Pressure review needs you", f"{label}: {reason}", repo_id=repo["id"], level="action")
        return

    gain = chosen["gain"]
    ui("check", f"Simulation · p95 {before['p95_ms'] / 1000:.1f} s → {chosen['after']['p95_ms'] / 1000:.1f} s · capacity +{gain['capacity_pct']}%", check={
        "id": "simulation", "group": "build", "name": "traffic simulation with the new topology", "phase": "patched", "status": "passed",
        "detail": f"p95 −{gain['p95_pct']}% · load {before['load']} → {chosen['after']['load']}"})
    diff = gitops.diff(workdir, [f.path for f in result.files])
    title = migration["title"]
    gitops.commit(workdir, title)
    skipped = [c["name"] for c in baseline["checks"] if not c["passed"]]
    body = "\n".join([
        f"## {title}", "", review["diagnosis"], "", f"**Change:** {chosen['title']}. {chosen['summary']}", "", f"**Why this one:** {review['why']}", "",
        "### Measured, then simulated", "| | Before | After (simulated) |", "|---|---|---|",
        f"| p95 latency | {before['p95_ms'] / 1000:.2f} s | {chosen['after']['p95_ms'] / 1000:.2f} s |",
        f"| load (busy share of the concurrency budget) | {before['load']} | {chosen['after']['load']} |",
        f"| sustainable calls/s | {before['capacity_rps']} | {chosen['after']['capacity_rps']} |", "",
        f"Traffic source: **{snap['source']}**. The after column is a queueing-model prediction (M/M/c on the observed rates), not a measurement. "
        "It will be re-measured on real traffic after the merge.", "",
        "### Checks", *[f"- {'✅' if c['passed'] else '❌'} `{c['cmd']}`: {c['summary']}" for c in (after or {}).get("checks", [])],
        *([f"- ⚠️ not used as a gate, already failing before this change: {', '.join(skipped)}"] if skipped else []), "",
        *(["### Please double check", *[f"- {r}" for r in [*review["risks"], *result.open_questions]], ""] if review["risks"] or result.open_questions else []),
        "---", "Opened by Chowkidaar after a traffic audit. Review before merging; Chowkidaar never merges."])
    save(diff=diff, branch=branch, validation={"before": baseline, "after": after, "simulation": {"before": before, "after": chosen["after"]}})

    pr_url = None
    ui("stage", stage="pr")
    if repo.get("full_name") and settings.open_prs:
        pr = gitops.open_pull_request(workdir, full_name=repo["full_name"], branch=branch, base=repo["default_branch"], title=title, body=body)
        pr_url = pr["url"]
        patches = ui_events.build_patches(diff, file_node)
        save(pr_url=pr_url, pr_number=pr["number"], pr_state="open", review={"seen": [], "rounds": []})
        ui("pr", f"GitHub pull request opened · #{pr['number']}", pr={"number": pr["number"], "title": title, "branch": branch, "base": repo["default_branch"],
           "url": pr_url, "files": len(patches), "additions": sum(p["additions"] for p in patches), "deletions": sum(p["deletions"] for p in patches)})
    save(status="pr_opened" if pr_url else "ready_local")
    ui("done", "Ready for review" if pr_url else f"Branch {branch} ready locally")
    events.emit("review.completed", f"{title}: {'opened ' + pr_url if pr_url else 'branch ready locally'}", **ids)
    agents.remember(repo["id"], "outcome", f"{title}: {chosen['title']}; " + (f"PR {pr_url}" if pr_url else f"branch {branch}"))
    notify.send("Pull request opened" if pr_url else "Change validated", f"{title} · simulated p95 −{gain['p95_pct']}%", repo_id=repo["id"], level="success", link=pr_url)
