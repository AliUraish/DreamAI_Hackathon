"""The typed event stream the interactive UI plays back (frontend/src/lib/types.ts `PipelineEvent`).

Separate from the activity feed in events.py: that one is a human-readable log,
this one carries the structured payloads (changes, trace hits, doc excerpts,
file patches, per-test checks) the graph view animates. Events are sent without
`at`; the UI stamps them on arrival.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import networkx as nx

from . import db

_subscribers: set[asyncio.Queue] = set()
_loop: asyncio.AbstractEventLoop | None = None


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def subscribe() -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue()
    _subscribers.add(queue)
    return queue


def unsubscribe(queue: asyncio.Queue) -> None:
    _subscribers.discard(queue)


def broadcast(event: dict[str, Any]) -> None:
    """Send to every open UI without storing: agent status, notifications, questions."""
    event = {k: v for k, v in event.items() if v is not None}
    if _loop and _loop.is_running():
        for queue in list(_subscribers):
            _loop.call_soon_threadsafe(queue.put_nowait, event)


def emit(migration_id: str, event: dict[str, Any], *, repo_id: str | None = None) -> None:
    """A run's event: stored (so the run can be replayed) and sent. Every event names its repository and run."""
    event = {k: v for k, v in {**event, "repoId": repo_id, "runId": migration_id}.items() if v is not None}
    db.insert("events", {"id": db.new_id("ui"), "repo_id": repo_id, "migration_id": migration_id, "ts": db.now(), "type": "ui",
                         "message": event.get("msg"), "data": event})
    broadcast(event)


def replay(migration_id: str) -> list[dict[str, Any]]:
    """A run's events with `offsetMs`: when each one really happened, measured from the first. Timestamps are stored to the
    second, so events inside the same second are spread evenly across it. A replay can then take as long as the real run did."""
    from datetime import datetime
    rows = db.events_after(0, {"migration_id": migration_id, "type": "ui"}, limit=2000)
    if not rows:
        return []
    start = datetime.fromisoformat(rows[0]["ts"])
    seconds = [int((datetime.fromisoformat(r["ts"]) - start).total_seconds()) for r in rows]
    out = []
    for index, row in enumerate(rows):
        same = [i for i, sec in enumerate(seconds) if sec == seconds[index]]
        out.append({**row["data"], "offsetMs": seconds[index] * 1000 + int(1000 * same.index(index) / len(same))})
    return out


# --- builders ---------------------------------------------------------------

_SEMANTIC_WORDS = re.compile(r"\b(unit|units|cents|integer|decimal|format|timezone|utc|iso|encoding|seconds|milliseconds|enum|precision|currency)\b", re.I)


def _blocks(markdown: str) -> list[tuple[str, str]]:
    """(section heading, block text) for each paragraph / list item of a markdown doc."""
    section, blocks, current = "", [], []

    def flush():
        if current:
            blocks.append((section, " ".join(current).strip()))
            current.clear()

    in_code = False
    for raw in markdown.splitlines():
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            flush()
            continue
        if in_code:
            continue
        if line.startswith("#"):
            flush()
            section = line.lstrip("# ").strip()
        elif not line.strip():
            flush()
        elif re.match(r"^\s*(\d+\.|[-*])\s+", line):
            flush()
            current.append(re.sub(r"^\s*(\d+\.|[-*])\s+", "", line))
        else:
            current.append(line.strip())
    flush()
    return blocks


def _mentions(text: str, token: str) -> bool:
    return bool(token) and re.search(rf"(?<![\w/]){re.escape(token)}(?![\w])", text) is not None


def _nodes_mentioning(graph: nx.Graph, files: list[dict[str, Any]], token: str, included: set[str]) -> list[str]:
    """Pipeline nodes (files and the symbols declared in them) whose file uses `token`."""
    touched = {f["path"] for f in files if not f.get("read_only") and _mentions(f["content"], token)}
    return sorted(n for n in included if graph.nodes[n].get("source_file") in touched)


def build_changes(changes: list[dict[str, Any]], docs_text: str | None, graph: nx.Graph, files: list[dict[str, Any]],
                  included: set[str], call_site_nodes: list[str]) -> list[dict[str, Any]]:
    """Contract changes in the UI's `BreakingChange` shape. A rename whose docs
    paragraph talks about units/format is marked `semantic`: the shape diff
    cannot see that, only the migration guide can."""
    blocks = _blocks(docs_text or "")
    out: list[dict[str, Any]] = []
    renamed_from = {c["path"] for c in changes if c["kind"] == "rename-candidate"}
    gone = next((c for c in changes if c["kind"] == "endpoint-gone"), None)

    for change in changes:
        kind = change["kind"]
        if kind == "endpoint-changed":
            note = f"{change['before']} now returns {gone['after']}." if gone else None
            out.append({"id": "endpoint", "kind": "endpoint", "label": "endpoint moved", "before": change["before"], "after": change["after"],
                        "where": "paths", "note": note, "nodeIds": call_site_nodes})
        elif kind == "rename-candidate":
            doc = next((text for _, text in blocks if _mentions(text, change["after"]) and _mentions(text, change["before"])), None)
            semantic = bool(doc and _SEMANTIC_WORDS.search(doc))
            note = None
            if semantic:
                note = "Meaning changes too, not only the name. Stated in the migration guide; invisible in the shape diff."
            elif not doc:
                note = "Candidate rename inferred from shape; not confirmed by documentation."
            out.append({"id": change["before"], "kind": "semantic" if semantic else "response",
                        "label": "renamed + meaning changed" if semantic else "field renamed", "before": change["before"], "after": change["after"],
                        "where": change["path"], "note": note, "nodeIds": _nodes_mentioning(graph, files, change["before"], included)})
        elif kind == "field-removed" and change["path"] not in renamed_from:
            field = change["path"].rsplit(".", 1)[-1]
            out.append({"id": field, "kind": "response", "label": "field removed", "before": field, "after": "(removed)", "where": change["path"],
                        "nodeIds": _nodes_mentioning(graph, files, field, included)})
        elif kind == "type-changed":
            field = change["path"].rsplit(".", 1)[-1]
            out.append({"id": f"type:{field}", "kind": "response", "label": "type changed", "before": f"{field}: {change['before']}", "after": f"{field}: {change['after']}",
                        "where": change["path"], "nodeIds": _nodes_mentioning(graph, files, field, included)})
        elif kind == "provider-switched":
            out.append({"id": "provider", "kind": "semantic", "label": "provider switched", "before": change["before"], "after": change["after"], "where": "environment",
                        "note": "A different API: requests, responses and authentication all change.", "nodeIds": call_site_nodes})
        elif kind == "env-renamed":
            out.append({"id": "env", "kind": "request", "label": "env variable renamed", "before": change["before"], "after": change["after"], "where": ".env",
                        "nodeIds": _nodes_mentioning(graph, files, change["before"], included)})
        elif kind == "endpoint-gone" and not any(c["kind"] == "endpoint-changed" for c in changes):
            out.append({"id": "endpoint", "kind": "endpoint", "label": "endpoint gone", "before": change["path"], "after": f"{change['after']} (no successor advertised)",
                        "where": "paths", "nodeIds": call_site_nodes})
    return out


def build_excerpts(ui_changes: list[dict[str, Any]], docs_text: str | None, limit: int = 6) -> list[dict[str, Any]]:
    excerpts = []
    for section, text in _blocks(docs_text or ""):
        # An endpoint change is only "about" a paragraph that names the old path; the new
        # path shows up in every example.
        matched = [c for c in ui_changes if _mentions(text, c["before"]) or (c["kind"] != "endpoint" and _mentions(text, c["after"]))]
        if not matched or len(text) < 25:
            continue
        bold = re.match(r"\*\*(.+?)\*\*", text)
        clean = re.sub(r"[*`]", "", text)
        excerpts.append({"id": f"d{len(excerpts) + 1}", "section": re.sub(r"[*`]", "", bold.group(1)).rstrip(".") if bold else section, "text": clean[:420],
                         "changeIds": [c["id"] for c in matched], "nodeIds": sorted({n for c in matched for n in c["nodeIds"]})})
        if len(excerpts) == limit:
            break
    return excerpts


def build_patches(diff: str, file_node: dict[str, str]) -> list[dict[str, Any]]:
    """Unified git diff -> the UI's `FilePatch[]` (one per file, hunks of typed lines)."""
    patches: list[dict[str, Any]] = []
    current, hunk = None, None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            path = line.split(" b/", 1)[-1]
            current = {"nodeId": file_node.get(path, path), "path": path, "additions": 0, "deletions": 0, "hunks": []}
            patches.append(current)
            hunk = None
        elif current is None or line.startswith(("index ", "--- ", "+++ ", "new file", "deleted file", "similarity", "rename ")):
            continue
        elif line.startswith("@@"):
            hunk = {"header": line, "lines": []}
            current["hunks"].append(hunk)
        elif hunk is not None and line[:1] in {" ", "+", "-"}:
            hunk["lines"].append({"t": line[0], "s": line[1:]})
            current["additions"] += line[0] == "+"
            current["deletions"] += line[0] == "-"
    return patches


def build_checks(validation: dict[str, Any], phase: str, test_node: dict[str, str], status_override: str | None = None) -> list[dict[str, Any]]:
    groups = {"tests": "tests", "typecheck": "types", "build": "build"}
    checks = []
    for check in validation.get("checks", []):
        group = groups.get(check["name"], "build")
        if group == "tests" and check.get("tests"):
            for index, test in enumerate(check["tests"], 1):
                checks.append({"id": f"t{index}", "group": "tests", "name": test["name"], "phase": phase, "nodeId": test_node.get(test["file"]), "cmd": check["cmd"],
                               "status": status_override or ("passed" if test["passed"] else "failed"),
                               "detail": None if status_override or test["passed"] else test["detail"]})
        else:
            tail = [line for line in (check.get("output") or "").splitlines() if line.strip()][-6:]
            checks.append({"id": check["name"], "group": group, "name": check["cmd"].replace(" --silent", ""), "phase": phase, "cmd": check["cmd"].replace(" --silent", ""),
                           "status": status_override or ("passed" if check["passed"] else "failed"),
                           "detail": None if status_override else check["summary"],
                           "durationMs": None if status_override else int(check.get("duration_s", 0) * 1000), "output": None if status_override else tail})
    return checks
