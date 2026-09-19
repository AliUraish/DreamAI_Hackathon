"""Traffic per node of the pipeline graph: measured, or simulated, never mixed up.

Spans come from two places. A connected project can report them (`POST /api/v1/traffic`,
the reporter in `app/static/chowkidaar-traffic.ts`), or the simulator can generate them from the
project's real call graph when the project is not deployed anywhere we can see. Every snapshot says
which one it is (`source: "live" | "simulated"`), and the UI labels it.

The same queueing model drives the simulator and the what-if numbers in a review, so a predicted
gain is comparable with the pressure that triggered it:

    load  rho = lambda * S / c      arrival rate x service time / concurrency budget
    wait  Wq  = ErlangC(c, lambda*S) * S / (c * (1 - rho))          (M/M/c)
    p95   ~  ln(20) * (S + Wq)      exponential tail
"""

from __future__ import annotations

import math
import random
import threading
import time
import zlib
from collections import defaultdict, deque
from typing import Any

import networkx as nx

from .scanner import ProviderUsage

WINDOW_SECONDS = 300
DEFAULT_BUDGET = 32          # concurrent executions a plain function can absorb before it queues
OUTBOUND_BUDGET = 6          # simultaneous outbound connections of one Cloudflare Worker invocation; the default for an API call site
PROVIDER_LATENCY_MS = {"llm": 1500, "database": 45, "auth": 70, "payments": 320, "messaging": 260, "voice": 900, "orders": 60}


# --- the queueing model -----------------------------------------------------------------


def erlang_c(servers: int, offered: float) -> float:
    """Probability that an arrival has to wait in an M/M/c queue (offered load a = lambda * S)."""
    if offered <= 0:
        return 0.0
    if offered >= servers:
        return 1.0
    term, total = 1.0, 1.0
    for k in range(1, servers):
        term *= offered / k
        total += term
    last = term * offered / servers / (1 - offered / servers)
    return last / (total + last)


def response(rate: float, service_ms: float, budget: int) -> dict[str, float]:
    """Steady-state numbers for one node. `saturated` means the queue grows without bound: requests time out."""
    service_s = service_ms / 1000
    offered = rate * service_s
    rho = offered / budget if budget else 0.0
    if rho >= 0.999:
        # Past saturation there is no steady state. Report what a 30 s client timeout would turn it into.
        return {"load": round(rho, 3), "wait_ms": 30000.0, "p95_ms": 30000.0, "capacity_rps": round(budget / service_s, 2), "saturated": True}
    wait_s = erlang_c(budget, offered) * service_s / (budget * (1 - rho)) if rate > 0 else 0.0
    return {"load": round(rho, 3), "wait_ms": round(wait_s * 1000, 1), "p95_ms": round(math.log(20) * (service_s + wait_s) * 1000, 1),
            "capacity_rps": round(budget / service_s, 2) if service_s else 0.0, "saturated": False}


# --- what was observed ---------------------------------------------------------------------


class _Repo:
    def __init__(self) -> None:
        self.spans: deque[tuple[float, str, str | None, float, bool, float]] = deque()  # ts, node, parent, ms, ok, busy_ms
        self.source = "none"
        self.budgets: dict[str, int] = {}
        self.lock = threading.Lock()


_repos: dict[str, _Repo] = defaultdict(_Repo)


def record(repo_id: str, spans: list[dict[str, Any]], source: str) -> None:
    repo = _repos[repo_id]
    now = time.time()
    with repo.lock:
        repo.source = source
        for s in spans:
            # `busy_ms` is the part of a call that occupies a slot. A reporter cannot tell waiting from working, so it defaults
            # to the whole duration, which overstates load a little under pressure; the simulator knows the difference.
            repo.spans.append((float(s.get("ts") or now), s["node"], s.get("parent"), float(s["ms"]), bool(s.get("ok", True)), float(s.get("busy_ms", s["ms"]))))
        while repo.spans and repo.spans[0][0] < now - WINDOW_SECONDS:
            repo.spans.popleft()


def set_budgets(repo_id: str, budgets: dict[str, int]) -> None:
    _repos[repo_id].budgets = dict(budgets)


def clear(repo_id: str) -> None:
    _repos.pop(repo_id, None)


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def snapshot(repo_id: str, window: int = 30) -> dict[str, Any]:
    """Per node: calls per second, latency, error rate and load over the last `window` seconds. Per link: calls per second."""
    repo = _repos.get(repo_id)
    if repo is None:
        return {"source": "none", "window": window, "nodes": {}, "links": {}}
    now = time.time()
    with repo.lock:
        recent = [s for s in repo.spans if s[0] >= now - window]
        budgets, source = dict(repo.budgets), repo.source
    by_node: dict[str, list[tuple[float, bool]]] = defaultdict(list)
    by_link: dict[tuple[str, str], list[float]] = defaultdict(list)
    busy: dict[str, float] = defaultdict(float)
    for _, node, parent, ms, ok, busy_ms in recent:
        busy[node] += busy_ms
        by_node[node].append((ms, ok))
        if parent:
            by_link[(parent, node)].append(ms)
    nodes = {}
    for node, samples in by_node.items():
        durations = [ms for ms, _ in samples]
        rps = len(samples) / window
        mean = sum(durations) / len(durations)
        budget = budgets.get(node, DEFAULT_BUDGET)
        nodes[node] = {"rps": round(rps, 2), "mean_ms": round(mean, 1), "p50_ms": round(_percentile(durations, 0.5), 1),
                       "p95_ms": round(_percentile(durations, 0.95), 1), "errors": round(1 - sum(ok for _, ok in samples) / len(samples), 3),
                       "budget": budget, "load": round(busy[node] / 1000 / window / budget, 3), "calls": len(samples)}  # busy slot-seconds per second, per slot
    links = {f"{a}->{b}": {"rps": round(len(ms) / window, 2), "mean_ms": round(sum(ms) / len(ms), 1)} for (a, b), ms in by_link.items()}
    return {"source": source if recent else "none", "window": window, "nodes": nodes, "links": links}


# --- the simulator -----------------------------------------------------------------------------


class Simulator:
    """Generates spans along a project's real call graph. One thread per project.

    profile "steady": every entry point gets a modest rate. profile "pressure": the entry points that
    reach `target` are multiplied until the target's load passes 1, which is what an audit looks for."""

    def __init__(self, repo_id: str, graph: nx.Graph, usages: dict[str, ProviderUsage], providers: dict[str, dict[str, Any]], seed: int = 7):
        self.repo_id, self.rng = repo_id, random.Random(seed)
        self.profile, self.target = "steady", None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.calls: dict[str, list[str]] = defaultdict(list)     # caller -> callees (functions and providers)
        self.service_ms: dict[str, float] = {}                   # a node's own work, or a provider's latency
        self.budgets: dict[str, int] = {}
        self._build(graph, usages, providers)

    def _build(self, graph: nx.Graph, usages: dict[str, ProviderUsage], providers: dict[str, dict[str, Any]]) -> None:
        from .graph import _find_nodes, affected_from_usage
        included: set[str] = set()
        for usage in usages.values():
            included |= set(affected_from_usage(graph, usage))
        for source, target, data in graph.edges(data=True):
            if data.get("relation") == "calls" and source in included and target in included:
                self.calls[source].append(target)
        for provider_id, usage in usages.items():
            node = f"provider:{provider_id}"
            category = (providers.get(provider_id) or {}).get("category", "")
            self.service_ms[node] = PROVIDER_LATENCY_MS.get(category, 200)
            _, symbols = _find_nodes(graph, usage)
            for symbol in symbols:
                self.calls[symbol].append(node)
                self.budgets[symbol] = OUTBOUND_BUDGET
        for node in included:
            self.service_ms.setdefault(node, 4 + (zlib.crc32(node.encode()) % 9))
        # The same API call costs different callers different amounts: a vision or document request holds the
        # connection far longer than a short text one. That asymmetry is what makes a shared call site worth splitting.
        self.factor: dict[tuple[str, str], float] = {}
        for caller, callees in self.calls.items():
            heavy = any(word in str(graph.nodes[caller].get("label", "")).lower() for word in ("image", "vision", "analy", "pdf", "render")) if caller in graph else False
            for callee in callees:
                if callee in self.budgets:
                    self.factor[(caller, callee)] = 2.6 if heavy else 1.0
        callees = {c for targets in self.calls.values() for c in targets}
        self.entries = sorted(n for n in self.calls if n not in callees)
        set_budgets(self.repo_id, self.budgets)

    def slot_ms(self, caller: str | None, node: str) -> float:
        """How long one call from `caller` occupies `node`, not counting any wait for a free slot."""
        factor = self.factor.get((caller, node), 1.0) if caller else 1.0
        return self.service_ms[node] + sum(self.slot_ms(None, c) * (factor if c.startswith("provider:") else 1.0) for c in self.calls.get(node, []))

    def reaches(self, start: str, goal: str) -> bool:
        seen, stack = set(), [start]
        while stack:
            node = stack.pop()
            if node == goal:
                return True
            if node not in seen:
                seen.add(node)
                stack.extend(self.calls.get(node, []))
        return False

    def _propagate(self, scale: float) -> dict[str, float]:
        rate: dict[str, float] = defaultdict(float)
        for entry in self.entries:
            base = 0.2 + (zlib.crc32(entry.encode()) % 5) * 0.05  # stable across runs, unlike hash()
            if scale != 1.0 and self.target and self.reaches(entry, self.target):
                base *= scale
            stack = [(entry, base)]
            while stack:
                node, r = stack.pop()
                rate[node] += r
                stack.extend((callee, r) for callee in self.calls.get(node, []))
        return rate

    def offered(self, node: str, rate: dict[str, float]) -> float:
        """Slots of `node` kept busy on average: sum over callers of rate x time per call."""
        return sum(rate[c] * self.slot_ms(c, node) / 1000 for c, targets in self.calls.items() if node in targets)

    def rates(self) -> dict[str, float]:
        """Calls per second at every node, propagated down from the entry points. Under the "pressure" profile the
        entry points that reach the target are scaled until the target runs at its budget (98%): busy enough that
        calls queue for seconds, not so far past it that nothing ever completes."""
        rate = self._propagate(1.0)
        if self.profile == "pressure" and self.target:
            busy = self.offered(self.target, rate)
            if busy > 0:
                rate = self._propagate(max(1.0, 0.98 * self.budgets.get(self.target, DEFAULT_BUDGET) / busy))
        return rate

    def wait_ms(self, node: str, rate: dict[str, float]) -> float:
        """Queueing delay at `node`, from the mix of callers it serves right now."""
        if node.startswith("provider:") or rate[node] <= 0:
            return 0.0
        callers = [c for c, targets in self.calls.items() if node in targets]
        weight = sum(rate[c] for c in callers)
        mean_slot = sum(rate[c] * self.slot_ms(c, node) for c in callers) / weight if weight else self.slot_ms(None, node)
        return min(response(rate[node], mean_slot, self.budgets.get(node, DEFAULT_BUDGET))["wait_ms"], 30000.0)

    def tick(self, seconds: float = 1.0) -> list[dict[str, Any]]:
        rate, spans, now = self.rates(), [], time.time()
        waits = {node: self.wait_ms(node, rate) for node in rate}
        for caller, callees in list(self.calls.items()) + [(None, self.entries)]:
            for node in callees:
                r = rate[node] if caller is None else rate[caller]
                slot, wait = self.slot_ms(caller, node), waits.get(node, 0.0)
                load = self.offered(node, rate) / self.budgets.get(node, DEFAULT_BUDGET) if node in self.budgets else 0.0
                for _ in range(self._poisson(r * seconds)):
                    # Erlang-4 rather than exponential: real call times cluster around their mean, and a 30 s window of
                    # exponential draws is too noisy to judge a load by.
                    busy = sum(self.rng.expovariate(4 / slot) for _ in range(4)) if slot > 0 else 0.0
                    queued = self.rng.expovariate(1 / wait) if wait > 0 else 0.0
                    spans.append({"node": node, "parent": caller, "ms": min(busy + queued, 30000.0), "busy_ms": busy,
                                  "ok": self.rng.random() > max(0.0, (load - 0.9) * 0.5), "ts": now})
        return spans

    def _poisson(self, mean: float) -> int:
        if mean <= 0:
            return 0
        limit, k, p = math.exp(-min(mean, 30)), 0, 1.0
        while True:
            p *= self.rng.random()
            if p <= limit:
                return k
            k += 1

    def warm(self, seconds: int = 60) -> None:
        """Fill the window at once (tests, and so a freshly opened UI is not empty). Replaces what was there:
        warming twice must not count the same seconds twice."""
        with _repos[self.repo_id].lock:
            _repos[self.repo_id].spans.clear()
        now = time.time()
        for back in range(seconds, 0, -1):
            spans = self.tick()
            for s in spans:
                s["ts"] = now - back
            record(self.repo_id, spans, "simulated")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def run() -> None:
            while not self._stop.wait(1.0):
                record(self.repo_id, self.tick(), "simulated")

        self._thread = threading.Thread(target=run, name=f"traffic-{self.repo_id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


_simulators: dict[str, Simulator] = {}


def simulator(repo_id: str) -> Simulator | None:
    return _simulators.get(repo_id)


def start_simulation(repo_id: str, graph: nx.Graph, usages: dict[str, ProviderUsage], providers: dict[str, dict[str, Any]],
                     profile: str = "steady", target: str | None = None) -> Simulator:
    sim = _simulators.get(repo_id)
    if sim is None:
        sim = _simulators[repo_id] = Simulator(repo_id, graph, usages, providers)
        sim.warm(45)
    sim.profile, sim.target = profile, target
    sim.start()
    return sim


def stop_simulation(repo_id: str) -> None:
    sim = _simulators.pop(repo_id, None)
    if sim:
        sim.stop()
    clear(repo_id)
