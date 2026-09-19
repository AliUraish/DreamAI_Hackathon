"""Explains a node or a connection of the pipeline graph in plain language.

The facts come from the graph and the code around the edge; the model only words
them. Without a model the same facts are returned as sentences, so the feature
never depends on a key being configured.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import networkx as nx

from . import agents, db, llm
from .scanner import ProviderUsage

SYSTEM = """You explain one piece of an API integration pipeline to the developer who owns the code.
You are given facts extracted from the code graph and a few lines of source. In 3 to 5 short sentences say what this node or
connection does in the pipeline, why it matters for the external API, and what would break here if that API changed.
Use the real names from the code. Plain prose, no lists, no markdown headings. Do not invent anything the facts do not support."""

RELATION_WORDS = {"calls": "calls", "imports_from": "imports from", "contains": "declares", "calls_api": "calls the external API",
                  "inherits": "inherits from", "extends": "extends", "implements": "implements", "re_exports": "re-exports"}


def _snippet(root: Path, source_file: str | None, location: str | None, radius: int = 9) -> str:
    if not source_file or not (root / source_file).is_file():
        return ""
    lines = (root / source_file).read_text(encoding="utf-8", errors="ignore").splitlines()
    line = int(location[1:]) if location and location[1:].isdigit() else 1
    start, end = max(0, line - 1 - 3), min(len(lines), line - 1 + radius)
    return "\n".join(f"{i + 1:>4} {lines[i]}" for i in range(start, end))


def _label(graph: nx.Graph, node_id: str, usages: dict[str, ProviderUsage]) -> str:
    if node_id.startswith("provider:"):
        usage = usages.get(node_id.split(":", 1)[1])
        return f"the external API {usage.name if usage else node_id.split(':', 1)[1]}"
    data = graph.nodes.get(node_id, {})
    return f"`{data.get('label', node_id)}` ({data.get('source_file')})"


def _facts_for_node(graph: nx.Graph, node_id: str, usages: dict[str, ProviderUsage], distances: dict[str, int]) -> list[str]:
    if node_id.startswith("provider:"):
        usage = usages.get(node_id.split(":", 1)[1])
        if not usage:
            return ["This provider was confirmed by the user, but no code calls it yet."]
        sites = [c for c in usage.call_sites if not c.is_test]
        facts = [f"{usage.name} is an external API this repository depends on."]
        facts += [f"`{c.function or c.file}` in {c.file}:{c.line} sends {c.method} {c.path or c.url}." for c in sites]
        if usage.sdk_files:
            facts.append("Its SDK or credentials are used in " + ", ".join(usage.sdk_files) + ".")
        return facts
    data = graph.nodes[node_id]
    facts = [f"{_label(graph, node_id, usages)} is {distances.get(node_id, '?')} hop(s) from the external API call."]
    for _, target, d in graph.out_edges(node_id, data=True):
        if target in distances and d.get("relation") in RELATION_WORDS:
            facts.append(f"It {RELATION_WORDS[d['relation']]} {_label(graph, target, usages)} at {d.get('source_file')}:{d.get('source_location')}.")
    for source, _, d in graph.in_edges(node_id, data=True):
        if source in distances and d.get("relation") in RELATION_WORDS and d["relation"] != "contains":
            facts.append(f"{_label(graph, source, usages)} {RELATION_WORDS[d['relation']]} it at {d.get('source_file')}:{d.get('source_location')}.")
    for usage in usages.values():
        for c in usage.call_sites:
            if c.file == data.get("source_file") and c.function and str(data.get("label", "")).removesuffix("()") == c.function:
                facts.append(f"This function is an API call site: it sends {c.method} {c.path or c.url} to {usage.name} (line {c.line}).")
    return facts


def _fallback(facts: list[str], impact: str) -> str:
    return " ".join(facts[:5]) + " " + impact


def explain(repo: dict[str, Any], graph: nx.Graph, usages: dict[str, ProviderUsage], distances: dict[str, int], *,
            node_id: str | None = None, source: str | None = None, target: str | None = None, mode: str = "ai") -> dict[str, Any]:
    """mode="facts": answer at once from the graph (or from the cache if the model already wrote this one).
    mode="ai": have the model word it; falls back to the facts if no model is available."""
    root = Path(repo["local_path"])
    if node_id:
        key, facts = f"node:{node_id}", _facts_for_node(graph, node_id, usages, distances)
        data = graph.nodes.get(node_id, {}) if not node_id.startswith("provider:") else {}
        snippet = _snippet(root, data.get("source_file"), data.get("source_location"))
        title = _label(graph, node_id, usages)
        impact = "If the API's contract changes, this is part of what Chowkidaar re-checks." if distances.get(node_id, 0) else "A change in the API's contract lands here first."
    else:
        key = f"link:{source}->{target}"
        edge = ({} if "provider:" in f"{source}{target}" else graph.get_edge_data(source, target) or graph.get_edge_data(target, source) or {})
        relation = "calls_api" if (target or "").startswith("provider:") else edge.get("relation", "references")
        title = f"{_label(graph, source, usages)} → {_label(graph, target, usages)}"
        facts = [f"{_label(graph, source, usages)} {RELATION_WORDS.get(relation, relation)} {_label(graph, target, usages)}"
                 + (f" at {edge.get('source_file')}:{edge.get('source_location')}." if edge else ".")]
        facts += _facts_for_node(graph, source, usages, distances)[1:4] if not source.startswith("provider:") else []
        anchor = graph.nodes.get(source, {})
        snippet = _snippet(root, edge.get("source_file") or anchor.get("source_file"), edge.get("source_location") or anchor.get("source_location"))
        impact = ("This is where the repository meets the external API: a contract change breaks here first." if relation == "calls_api"
                  else "This connection is how an API change travels through the code: whatever breaks on one side is felt on the other.")

    content_hash = hashlib.sha256(("\n".join(facts) + snippet).encode()).hexdigest()[:16]
    # Only what a model wrote is worth caching; the facts are recomputed in microseconds and must not block a later AI answer.
    cached = next((e for e in db.select("explanations", {"repo_id": repo["id"], "target": key})
                   if e["content_hash"] == content_hash and e["source"] != "graph"), None)
    if cached:
        return {"target": key, "title": title, "text": cached["text"], "source": cached["source"], "facts": facts, "cached": True, "ai_available": True}

    text, origin = _fallback(facts, impact), "graph"
    if mode == "facts" or not llm.provider():
        return {"target": key, "title": title, "text": text, "source": origin, "facts": facts, "cached": False, "ai_available": bool(llm.provider())}
    if llm.provider():
        notes = agents.recall(repo["id"])
        prompt = ("## Facts\n" + "\n".join(f"- {f}" for f in facts) + (f"\n\n## Source\n```\n{snippet}\n```" if snippet else "")
                  + (f"\n\n## Your notes on this repository\n{notes}" if notes else ""))
        try:
            text, origin = llm.text(SYSTEM, prompt), llm.provider() or "llm"
        except llm.LLMUnavailable:
            pass  # the graph facts are still a truthful answer
    if origin != "graph":
        for old in db.select("explanations", {"repo_id": repo["id"], "target": key}):
            db.delete("explanations", {"id": old["id"]})
        db.insert("explanations", {"id": db.new_id("exp"), "repo_id": repo["id"], "target": key, "content_hash": content_hash, "text": text,
                                   "source": origin, "created_at": db.now()})
    return {"target": key, "title": title, "text": text, "source": origin, "facts": facts, "cached": False, "ai_available": bool(llm.provider())}


# --- every connection, ahead of the click -------------------------------------------------

_prefetch: dict[str, dict[str, int]] = {}  # repo id -> {"done", "total", "failed"}


def prefetch_status(repo_id: str) -> dict[str, int]:
    return _prefetch.get(repo_id, {"done": 0, "total": 0, "failed": 0})


def prefetch_links(repo: dict[str, Any], graph: nx.Graph, usages: dict[str, ProviderUsage], distances: dict[str, int],
                   links: list[tuple[str, str]], workers: int = 4) -> None:
    """Have the model explain every connection of the pipeline graph in the background, a few at a time,
    so that a click answers at once. Skips what is already written; runs once per graph shape."""
    from concurrent.futures import ThreadPoolExecutor
    if not llm.provider() or not links or _prefetch.get(repo["id"], {}).get("total") == len(links):
        return
    status = _prefetch[repo["id"]] = {"done": 0, "total": len(links), "failed": 0}

    def one(pair: tuple[str, str]) -> None:
        try:
            result = explain(repo, graph, usages, distances, source=pair[0], target=pair[1])
            status["failed" if result["source"] == "graph" else "done"] += 1
        except Exception:
            status["failed"] += 1

    def run() -> None:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="explain") as pool:
            list(pool.map(one, links))

    import threading
    threading.Thread(target=run, name=f"explain-{repo['id']}", daemon=True).start()
