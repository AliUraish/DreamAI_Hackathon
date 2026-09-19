"""The integration pipeline graph: provider -> endpoint -> call site -> dependents.

Graphify (tree-sitter, local, no LLM) builds the code graph of the repository.
That full graph never leaves this module. What the API serves is the *slice*
that belongs to one maintained API: the call sites the scanner found, plus
everything Graphify says depends on them, up to `depth` hops. Nothing else in
the codebase appears.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import networkx as nx
from graphify.affected import affected_nodes, load_graph

from .config import settings
from .scanner import ProviderUsage

CODE_RELATIONS = {"calls", "indirect_call", "imports_from", "dynamic_import", "re_exports", "inherits", "extends", "implements", "contains"}


def build_code_graph(repo_root: Path, cache_key: str, force: bool = False) -> nx.Graph:
    """Run `graphify extract --code-only` and load the result. Output is kept
    under our data dir (`--out`), never inside the customer's repository."""
    from .scanner import iter_source_files
    if next(iter_source_files(repo_root), None) is None:
        return nx.DiGraph()  # nothing to parse (a config-only repository); Graphify exits non-zero on that
    out_dir = settings.data_dir / "graphs" / cache_key
    graph_file = out_dir / "graphify-out" / "graph.json"
    if force and out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    if not graph_file.exists():
        out_dir.mkdir(parents=True, exist_ok=True)
        graphify = Path(sys.executable).parent / "graphify"
        command = [str(graphify) if graphify.exists() else "graphify", "extract", str(repo_root), "--code-only", "--out", str(out_dir)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=300)
        if result.returncode != 0 or not graph_file.exists():
            raise RuntimeError(f"graphify extract failed: {(result.stderr or result.stdout)[-600:]}")
    return load_graph(graph_file)


def _line(data: dict[str, Any]) -> int | None:
    location = str(data.get("source_location") or "")
    return int(location[1:]) if location[1:].isdigit() else None


def _is_file_node(graph: nx.Graph, node_id: str) -> bool:
    return any(d.get("relation") == "contains" for _, _, d in graph.out_edges(node_id, data=True)) or _line(graph.nodes[node_id]) == 1 and not str(graph.nodes[node_id].get("label", "")).endswith("()")


def _find_nodes(graph: nx.Graph, usage: ProviderUsage) -> tuple[dict[str, str], dict[str, tuple[str, Any]]]:
    """Map the scanner's findings onto Graphify node ids.

    Returns (file path -> file node id, symbol node id -> (file, call site))."""
    seed_files = set(usage.files)
    file_nodes: dict[str, str] = {}
    symbol_nodes: dict[str, tuple[str, Any]] = {}
    by_function = {(c.file, c.function): c for c in usage.call_sites if c.function and not c.is_test}
    for node_id, data in graph.nodes(data=True):
        source_file = data.get("source_file")
        if source_file not in seed_files:
            continue
        label = str(data.get("label", ""))
        if _is_file_node(graph, node_id):
            file_nodes[source_file] = node_id
        call_site = by_function.get((source_file, label.removesuffix("()")))
        if call_site:
            symbol_nodes[node_id] = (source_file, call_site)
    return file_nodes, symbol_nodes


def affected_from_usage(graph: nx.Graph, usage: ProviderUsage, depth: int | None = None) -> dict[str, int]:
    """Graphify node id -> distance from the API call (0 = the call site itself)."""
    depth = depth or settings.graph_depth
    file_nodes, symbol_nodes = _find_nodes(graph, usage)
    distances: dict[str, int] = {}
    for seed in [*file_nodes.values(), *symbol_nodes]:
        distances[seed] = 0
        for hit in affected_nodes(graph, seed, depth=depth):
            distances[hit.node_id] = min(distances.get(hit.node_id, 99), hit.depth)
    # Everything declared in a call-site file is part of the integration surface
    # (its request/response types, for one).
    for file_node in file_nodes.values():
        for _, child, data in graph.out_edges(file_node, data=True):
            if data.get("relation") == "contains":
                distances.setdefault(child, 0)
    return distances


def affected_files(graph: nx.Graph, usage: ProviderUsage, depth: int | None = None) -> list[dict[str, Any]]:
    """Files in the blast radius, nearest first: [{path, depth, role}]."""
    best: dict[str, int] = {f: 0 for f in usage.files}
    for node_id, distance in affected_from_usage(graph, usage, depth).items():
        source_file = graph.nodes[node_id].get("source_file")
        if source_file:
            best[source_file] = min(best.get(source_file, 99), distance)
    return [
        {"path": path, "depth": distance, "role": "call_site" if path in usage.files else "dependent"}
        for path, distance in sorted(best.items(), key=lambda kv: (kv[1], kv[0]))
    ]


def pipeline_graph(graph: nx.Graph, usage: ProviderUsage, integration: dict[str, Any], *,
                   depth: int | None = None, changed_paths: set[str] | None = None,
                   patched_files: set[str] | None = None) -> dict[str, Any]:
    """The graph the UI renders for one integration. Only pipeline nodes.

    status per node: "healthy" | "breaking" | "affected" | "patched".
    """
    changed_paths = changed_paths or set()
    patched_files = patched_files or set()
    broken = integration.get("status") in {"breaking", "migrating", "migration_ready", "needs_review", "deprecated"}
    distances = affected_from_usage(graph, usage, depth)
    _, symbol_nodes = _find_nodes(graph, usage)

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    provider_id = f"provider:{usage.provider}"
    nodes.append({"id": provider_id, "kind": "provider", "label": usage.name, "status": "breaking" if broken else "healthy",
                  "version": integration.get("version"), "depth": -2})

    endpoint_ids: dict[tuple[str, str], str] = {}
    for call in usage.call_sites:
        if call.is_test or not call.path:
            continue
        key = (call.method, call.path)
        if key not in endpoint_ids:
            endpoint_id = f"endpoint:{call.method} {call.path}"
            endpoint_ids[key] = endpoint_id
            nodes.append({"id": endpoint_id, "kind": "endpoint", "label": f"{call.method} {call.path}", "method": call.method,
                          "path": call.path, "status": "breaking" if broken and call.path in changed_paths else "healthy", "depth": -1})
            edges.append({"id": f"{provider_id}->{endpoint_id}", "source": provider_id, "target": endpoint_id, "relation": "exposes"})

    for node_id, distance in distances.items():
        data = graph.nodes[node_id]
        source_file = data.get("source_file")
        is_file = _is_file_node(graph, node_id)
        if source_file in patched_files:
            status = "patched"
        elif broken:
            status = "affected"
        else:
            status = "healthy"
        nodes.append({
            "id": node_id, "kind": "file" if is_file else "symbol", "label": data.get("label"),
            "file": source_file, "line": _line(data), "depth": distance,
            "role": "call_site" if distance == 0 else "dependent", "status": status,
        })

    for symbol_id, (_, call) in symbol_nodes.items():
        endpoint_id = endpoint_ids.get((call.method, call.path))
        if endpoint_id:
            edges.append({"id": f"{symbol_id}->{endpoint_id}", "source": symbol_id, "target": endpoint_id,
                          "relation": "calls_api", "file": call.file, "line": call.line})

    included = set(distances)
    for source, target, data in graph.edges(data=True):
        relation = data.get("relation")
        if source in included and target in included and relation in CODE_RELATIONS:
            edges.append({"id": f"{source}-{relation}->{target}", "source": source, "target": target, "relation": relation,
                          "file": data.get("source_file"), "line": _line(data), "confidence": data.get("confidence")})

    files = sorted({n["file"] for n in nodes if n.get("file")})
    return {
        "integration_id": integration.get("id"), "provider": usage.provider, "depth": depth or settings.graph_depth,
        "nodes": nodes, "edges": edges,
        "stats": {"nodes": len(nodes), "edges": len(edges), "files": len(files),
                  "repo_nodes_total": graph.number_of_nodes(), "excluded_nodes": graph.number_of_nodes() - len(included)},
        "files": files,
    }


# --- views for the interactive UI ------------------------------------------------


def _community_names(graph: nx.Graph, node_ids: set[str]) -> dict[int, str]:
    """Graphify's code-only mode numbers communities but does not name them;
    name each after the directory most of its nodes live in."""
    from collections import Counter
    buckets: dict[int, Counter] = {}
    for node_id in node_ids:
        data = graph.nodes[node_id]
        parts = [p for p in Path(data.get("source_file") or "").parts[:-1] if p not in {"src", "app", "."}]
        buckets.setdefault(int(data.get("community") or 0), Counter())[parts[-1] if parts else "root"] += 1
    return {community: counter.most_common(1)[0][0] for community, counter in buckets.items()}


BROKEN_STATES = {"breaking", "migrating", "needs_review", "deprecated"}


def node_link_slice(graph: nx.Graph, slices: list[tuple[ProviderUsage, dict[str, Any]]], depth: int | None = None,
                    patched: dict[str, set[str]] | None = None) -> dict[str, Any]:
    """The pipeline slice in Graphify's own graph.json shape (NetworkX node-link),
    plus provider nodes (`file_type: "provider"`) joined to their callers by
    `calls_api` links. Only nodes in some maintained API's pipeline are included."""
    included: set[str] = set()
    nodes: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    order: dict[str, int] = {}   # when a node joins the picture: provider 0, call sites 1, then outwards hop by hop
    status: dict[str, str] = {}  # what the backend knows without a run on screen: red where an open issue reaches
    for usage, integration in slices:
        distances = affected_from_usage(graph, usage, depth)
        included |= set(distances)
        broken = integration.get("status") in BROKEN_STATES
        done = (patched or {}).get(integration.get("id") or "", set())
        for node_id, distance in distances.items():
            order[node_id] = min(order.get(node_id, 99), distance + 1)
            source_file = graph.nodes[node_id].get("source_file")
            if source_file in done and integration.get("status") == "migration_ready":
                status[node_id] = "patched"
            elif broken and status.get(node_id) != "patched":
                status[node_id] = "breaking" if distance == 0 else "affected"
        provider_id = f"provider:{usage.provider}"
        nodes.append({"id": provider_id, "label": usage.name, "file_type": "provider", "version": integration.get("version"),
                      "community": None, "integration_id": integration.get("id"), "integration_status": integration.get("status"), "order": 0,
                      "status": "breaking" if integration.get("status") in BROKEN_STATES else "patched" if integration.get("status") == "migration_ready" else "healthy"})
        file_nodes, symbol_nodes = _find_nodes(graph, usage)
        for symbol_id, (_, call) in symbol_nodes.items():
            links.append({"source": symbol_id, "target": provider_id, "relation": "calls_api",
                          "source_file": call.file, "source_location": f"L{call.line}", "endpoint": f"{call.method} {call.path}"})
        # An API reached through its SDK has no URL in the code to hang a call site on: the importing file is the call site.
        direct = {call.file for _, call in symbol_nodes.values()}
        for path, file_node in file_nodes.items():
            if path in usage.sdk_files and path not in direct:
                links.append({"source": file_node, "target": provider_id, "relation": "calls_api", "source_file": path, "source_location": "L1", "via": "sdk"})
    names = _community_names(graph, included)
    provider_community = {}
    for node_id in sorted(included):
        data = graph.nodes[node_id]
        community = int(data.get("community") or 0)
        nodes.append({"id": node_id, "label": data.get("label"), "file_type": data.get("file_type", "code"),
                      "source_file": data.get("source_file"), "source_location": data.get("source_location"),
                      "community": community, "community_name": names.get(community, f"community {community}"),
                      "order": order.get(node_id, 1), "status": status.get(node_id, "healthy")})
    for link in links:  # a provider sits in the community of the code that calls it
        provider_community.setdefault(link["target"], graph.nodes[link["source"]].get("community") or 0)
    for node in nodes:
        if node["file_type"] == "provider":
            node["community"] = int(provider_community.get(node["id"], 0))
            node["community_name"] = names.get(node["community"], "api")
    seen: set[tuple[str, str]] = set()
    for source, target, data in graph.edges(data=True):
        if source in included and target in included and data.get("relation") in CODE_RELATIONS and (source, target) not in seen:
            seen.add((source, target))
            links.append({"source": source, "target": target, "relation": data["relation"], "confidence": data.get("confidence"),
                          "source_file": data.get("source_file"), "source_location": data.get("source_location")})
    return {"directed": True, "multigraph": False, "graph": {"scope": "api-pipeline", "repo_nodes_total": graph.number_of_nodes()},
            "nodes": nodes, "links": links}


def trace_hits(graph: nx.Graph, usage: ProviderUsage, depth: int | None = None) -> list[dict[str, Any]]:
    """The blast radius as an ordered walk outward from the provider:
    [{nodeId, from, hop, role}], each node once, with the node it was reached from."""
    from graphify.affected import DEFAULT_AFFECTED_RELATIONS
    included = affected_from_usage(graph, usage, depth)
    file_nodes, symbol_nodes = _find_nodes(graph, usage)
    call_site_files = set(file_nodes.values())
    provider_id = f"provider:{usage.provider}"

    def role(node_id: str) -> str:
        data = graph.nodes[node_id]
        if any(marker in f"/{data.get('source_file') or ''}" for marker in ("/tests/", "/test/", "/__tests__/", ".test.", ".spec.")):
            return "test"
        if not _is_file_node(graph, node_id):
            return "symbol"
        return "change" if node_id in call_site_files else "dependent"

    hits, seen = [], set()
    direct = {call.file for _, call in symbol_nodes.values()}
    frontier = [(symbol_id, provider_id) for symbol_id in symbol_nodes] + [
        (node, provider_id) for path, node in file_nodes.items() if path not in direct]  # SDK users: the file itself meets the API
    hop = 1
    while frontier:
        next_frontier = []
        for node_id, parent in frontier:
            if node_id in seen or node_id not in included:
                continue
            seen.add(node_id)
            hits.append({"nodeId": node_id, "from": parent, "hop": hop, "role": role(node_id)})
            for source, _, data in graph.in_edges(node_id, data=True):
                # who depends on this node, and the file that declares it
                if data.get("relation") in DEFAULT_AFFECTED_RELATIONS or data.get("relation") == "contains":
                    next_frontier.append((source, node_id))
        frontier, hop = next_frontier, hop + 1
    hop_of = {h["nodeId"]: h["hop"] for h in hits}
    for node_id in included:  # declared in a call-site file but not on a dependency path (its types)
        if node_id not in seen:
            owner = next((s for s, _, d in graph.in_edges(node_id, data=True) if d.get("relation") == "contains"), provider_id)
            hits.append({"nodeId": node_id, "from": owner, "hop": hop_of.get(owner, 1) + 1, "role": role(node_id)})
    return hits
