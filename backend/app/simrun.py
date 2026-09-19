"""One button: simulate the project's traffic, measure every node, say what is good and what is bad, recommend.

A simulation is a what-if. It never opens a pull request by itself and the periodic audit does not act on it; the
recommendation it ends with can be applied by the user, and only then does the pressure review (perf.py) run.

Phases, each announced on the stream as {"t": "sim", phase, msg}:  steady -> ramp -> measure -> report
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field

from . import agents, db, llm, perf, traffic, ui_events
from .providers import all_providers, get_provider

_running: set[str] = set()


def exploring(repo_id: str) -> bool:
    """True while a what-if simulation drives this project's traffic: the audit must not mistake it for an incident."""
    return repo_id in _running


class Assessment(BaseModel):
    headline: str = Field(description="One sentence: the state of this pipeline under the simulated load.")
    good: list[str] = Field(description="2-4 short findings that are healthy, each naming a real node or provider and a number given to you.")
    bad: list[str] = Field(description="1-4 short findings that are weak, each naming a real node or provider and a number given to you.")
    recommendation: str = Field(description="Two sentences: what to change first and why, quoting the simulated before/after numbers of the option you were given.")


def _say(repo_id: str, phase: str, msg: str, **extra: Any) -> None:
    agents.progress(repo_id, msg)
    ui_events.broadcast({"t": "sim", "repoId": repo_id, "phase": phase, "msg": msg, **extra})


def start(repo: dict[str, Any], *, steady_seconds: float = 6, pressure_seconds: float = 13) -> bool:
    if repo["id"] in _running:
        return False
    _running.add(repo["id"])
    agents.submit(repo["id"], "Simulating traffic through the pipeline", lambda: _run(repo, steady_seconds, pressure_seconds))
    return True


def _run(repo: dict[str, Any], steady_seconds: float, pressure_seconds: float) -> None:
    from . import service
    try:
        usages, graph = service.analysis(repo)
        label = lambda n: str(graph.nodes[n].get("label", n)) if n in graph else n.removeprefix("provider:").title()  # noqa: E731
        providers = {p["id"]: p for p in all_providers()}

        traffic.stop_simulation(repo["id"])
        sim = traffic.start_simulation(repo["id"], graph, usages, providers, "steady")
        _say(repo["id"], "steady", f"Normal load: calls flow from {len(sim.entries)} entry point(s) through {len(sim.service_ms)} nodes")
        time.sleep(steady_seconds)
        calm = traffic.snapshot(repo["id"], window=int(steady_seconds) + 40)

        rate = sim.rates()
        shared = [n for n in sim.budgets if len([c for c, t in sim.calls.items() if n in t]) >= 2] or list(sim.budgets)
        target = max(shared, key=lambda n: sim.offered(n, rate)) if shared else None
        if target:
            sim.profile, sim.target = "pressure", target
            _say(repo["id"], "ramp", f"Raising load until {label(target)} runs at its budget", target=target)
            time.sleep(pressure_seconds)
        _say(repo["id"], "measure", "Measuring every node: calls per second, p95, load, errors")
        hot = traffic.snapshot(repo["id"], window=max(8, int(pressure_seconds) - 2))

        report = build_report(repo, graph, usages, sim, calm, hot, target, label)
        db.insert("audits", {"id": db.new_id("aud"), "repo_id": repo["id"], "verdict": "simulation", "source": "simulated", "window_seconds": hot["window"],
                             "summary": report["headline"], "findings": report["bad"], "metrics": report, "action": None, "created_at": db.now()})
        agents.remember(repo["id"], "outcome", f"Simulation: {report['headline']} Recommended: {report['recommendation']['title'] if report['recommendation'] else 'nothing'}.")
        _say(repo["id"], "report", report["headline"], report=report)
    finally:
        sim = traffic.simulator(repo["id"])
        if sim:  # back to normal load: the picture calms down and the run can be repeated
            sim.profile, sim.target = "steady", None
        _running.discard(repo["id"])


def build_report(repo, graph, usages, sim, calm, hot, target, label) -> dict[str, Any]:
    nodes = {n: m for n, m in hot["nodes"].items() if not n.startswith("provider:")}
    metrics = [{"node": n, "label": label(n), "rps": m["rps"], "p95_ms": m["p95_ms"], "load": m["load"], "errors": m["errors"], "budget": m["budget"],
                "calm_load": calm["nodes"].get(n, {}).get("load", 0.0)} for n, m in sorted(nodes.items(), key=lambda item: -item[1]["load"])]
    good: list[dict[str, Any]] = []
    bad: list[dict[str, Any]] = []

    healthy = [m for m in metrics if m["load"] < 0.6 and m["errors"] < 0.01]
    if healthy:
        good.append({"text": f"{len(healthy)} of {len(metrics)} functions stay under 60% load even at peak", "nodeIds": [m["node"] for m in healthy[:12]]})
    for m in metrics:
        if m["load"] >= perf.PRESSURE_LOAD:
            bad.append({"text": f"{m['label']} runs at load {m['load']:.2f} of {m['budget']} slots: calls queue, p95 {m['p95_ms'] / 1000:.1f} s", "nodeIds": [m["node"]], "severity": "pressure"})
        elif m["load"] >= 0.6:
            bad.append({"text": f"{m['label']} is at load {m['load']:.2f}, close to its budget", "nodeIds": [m["node"]], "severity": "watch"})
        if m["errors"] >= 0.02:
            bad.append({"text": f"{m['label']} fails {m['errors']:.0%} of calls under this load", "nodeIds": [m["node"]], "severity": "errors"})

    by_category: dict[str, list[str]] = {}
    for provider_id in usages:
        by_category.setdefault((get_provider(provider_id) or {}).get("category", "other"), []).append(provider_id)
    for provider_id, usage in usages.items():
        node, category = f"provider:{provider_id}", (get_provider(provider_id) or {}).get("category", "other")
        seen = hot["nodes"].get(node)
        sites = [n for n in sim.budgets if node in sim.calls.get(n, [])]
        others = [p for p in by_category[category] if p != provider_id]
        if seen and len(sites) == 1 and not others:
            bad.append({"text": f"{usage.name} is reached through one function, {label(sites[0])}, and no other {category} provider is wired in: a single point of failure",
                        "nodeIds": [node, sites[0]], "severity": "watch"})
        elif seen and others:
            good.append({"text": f"{usage.name} has an alternative already in the code ({', '.join((get_provider(p) or {}).get('name', p) for p in others)}): traffic can be rerouted",
                         "nodeIds": [node, *[f"provider:{p}" for p in others]]})
        if seen and seen["errors"] < 0.01:
            good.append({"text": f"{usage.name} answers {seen['rps']:.1f} calls/s with no failures", "nodeIds": [node]})

    recommendation = None
    if target and target in hot["nodes"]:
        stats, callers = hot["nodes"][target], perf.callers_of(hot, target)
        if callers:
            provider_id = next((pid for pid, u in usages.items() if f"provider:{pid}" in sim.calls.get(target, [])), None)
            slots = {c["node"]: sim.slot_ms(c["node"], target) for c in callers}
            before, options = perf.build_options(target, label(target), stats, callers, slots,
                                                 perf.alternative_providers(repo["id"], provider_id) if provider_id else [], {n: label(n) for n in graph.nodes})
            best = options[0]
            recommendation = {"nodeId": target, "label": label(target), "prefer": best["id"], "title": best["title"], "summary": best["summary"],
                              "before": before, "after": best["after"], "gain": best["gain"], "new_nodes": best["new_nodes"],
                              "alternatives": [{"title": o["title"], "p95_ms": o["after"]["p95_ms"], "gain": o["gain"]} for o in options[1:]]}

    headline = (f"{len([b for b in bad if b.get('severity') == 'pressure'])} node(s) under pressure at peak; {len(healthy)} of {len(metrics)} functions healthy"
                if bad else f"All {len(metrics)} functions stay healthy at peak load")
    narrative = None
    if llm.provider():
        facts = ("## Metrics at peak (simulated)\n" + "\n".join(f"- {m['label']}: {m['rps']} calls/s, p95 {m['p95_ms']:.0f} ms, load {m['load']} of {m['budget']} slots "
                                                                f"(normal load {m['calm_load']}), errors {m['errors']:.1%}" for m in metrics[:14])
                 + "\n\n## Healthy\n" + "\n".join(f"- {g['text']}" for g in good) + "\n\n## Weak\n" + "\n".join(f"- {b['text']}" for b in bad)
                 + (f"\n\n## Best simulated option\n{recommendation['title']}: {recommendation['summary']} p95 {recommendation['before']['p95_ms']:.0f} -> "
                    f"{recommendation['after']['p95_ms']:.0f} ms, capacity {recommendation['before']['capacity_rps']} -> {recommendation['after']['capacity_rps']} calls/s."
                    if recommendation else ""))
        try:
            narrative = llm.structured("You review a simulated load test of one service's API pipeline for its developer. Use only the facts and numbers given; "
                                       "never invent a number. Short, concrete, no hype.", facts, Assessment, effort="low")
        except llm.LLMUnavailable:
            narrative = None
    return {"headline": narrative.headline if narrative else headline, "source": "simulated", "metrics": metrics[:16],
            "good": [g for g in good][:5], "bad": bad[:6],
            "goodText": narrative.good if narrative else [g["text"] for g in good][:4], "badText": narrative.bad if narrative else [b["text"] for b in bad][:4],
            "advice": narrative.recommendation if narrative else (f"{recommendation['title']}. {recommendation['summary']}" if recommendation else "Nothing needs changing at this load."),
            "recommendation": recommendation, "writtenBy": llm.provider() if narrative else "simulation"}
