"""Structural diff between two schema snapshots, classified by how likely each
change is to break a caller.

Port of `@schema-watch/core` diff.ts (Apache-2.0), plus `detect_renames`, which
schema-watch does not have: a field removed and a field added at the same level
with the same type is a *candidate* rename. The idea comes from
api-schema-differentiator (MIT); the implementation is ours. A candidate is not
proof - the pipeline confirms it against the provider's migration docs.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from .infer import SchemaNode

Target = Literal["request", "response"]
Change = dict[str, Any]


def type_signature(node: SchemaNode) -> str:
    if node["kind"] == "unknown" and node["nullable"]:
        return "null"
    kind = node["kind"]
    if kind == "unknown":
        base = "unknown"
    elif kind == "primitive":
        base = node["type"]
    elif kind == "array":
        base = f"{type_signature(node['items']) if node['items'] else 'unknown'}[]"
    elif kind == "object":
        base = "object"
    else:
        base = " | ".join(sorted({type_signature(o) for o in node["options"]}))
    return f"{base} | null" if node["nullable"] else base


def diff_schemas(before: SchemaNode, after: SchemaNode, target: Target = "response") -> list[Change]:
    """Every structural change between two snapshots of the same body.

    Severity depends on `target`: a new required field breaks a *request* (callers
    do not send it yet) but is harmless in a *response*; a field that becomes
    optional breaks a *response* (consumers assumed it) but is safe in a request.
    """
    changes: list[Change] = []
    _walk("", before, after, target, changes)
    return changes


def _change(kind: str, path: str, severity: str, before: str | None, after: str | None) -> Change:
    return {"kind": kind, "path": path or "(root)", "severity": severity, "before": before, "after": after}


def _walk(path: str, before: SchemaNode, after: SchemaNode, target: Target, out: list[Change]) -> None:
    if not before["nullable"] and after["nullable"]:
        out.append(_change("became-nullable", path, "BREAKING", type_signature(before), type_signature(after)))
    elif before["nullable"] and not after["nullable"]:
        out.append(_change("became-non-nullable", path, "INFO", type_signature(before), type_signature(after)))

    if before["kind"] == "object" and after["kind"] == "object":
        for key in sorted({*before["properties"], *after["properties"]}):
            bp, ap = before["properties"].get(key), after["properties"].get(key)
            child = f"{path}.{key}" if path else key
            if bp and not ap:
                severity = "BREAKING" if bp["required"] else "WARNING"
                out.append(_change("field-removed", child, severity, type_signature(bp["schema"]), None))
            elif ap and not bp:
                severity = "BREAKING" if ap["required"] and target == "request" else "INFO"
                out.append(_change("field-added", child, severity, None, type_signature(ap["schema"])))
            else:
                sigs = (type_signature(bp["schema"]), type_signature(ap["schema"]))
                if bp["required"] and not ap["required"]:
                    out.append(_change("required-to-optional", child, "BREAKING" if target == "response" else "INFO", *sigs))
                elif not bp["required"] and ap["required"]:
                    out.append(_change("optional-to-required", child, "BREAKING" if target == "request" else "WARNING", *sigs))
                _walk(child, bp["schema"], ap["schema"], target, out)
        return

    if before["kind"] == "array" and after["kind"] == "array":
        if before["items"] and after["items"]:
            _walk(f"{path}[]", before["items"], after["items"], target, out)
        return

    if before["kind"] == "unknown" or after["kind"] == "unknown":
        return

    before_sig, after_sig = type_signature(before), type_signature(after)
    if before_sig != after_sig:
        out.append(_change("type-changed", path, "BREAKING", before_sig, after_sig))


def _parent(path: str) -> str:
    return path.rsplit(".", 1)[0] if "." in path else ""


def _leaf(path: str) -> str:
    return path.rsplit(".", 1)[-1]


def _tokens(name: str) -> set[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return {t for t in re.split(r"[^a-zA-Z0-9]+", spaced.lower()) if t}


def detect_renames(changes: list[Change]) -> list[Change]:
    """Pair removed and added fields at the same level with the same type.

    Shared name tokens (`name` / `customer_name`) rank a pair first; otherwise a
    pair is only proposed when it is the single same-typed candidate at that level.
    """
    removed = [c for c in changes if c["kind"] == "field-removed"]
    added = [c for c in changes if c["kind"] == "field-added"]
    candidates: list[Change] = []
    used: set[str] = set()
    for gone in removed:
        peers = [
            a for a in added
            if a["path"] not in used and _parent(a["path"]) == _parent(gone["path"]) and a["after"] == gone["before"]
        ]
        if not peers:
            continue
        shared = [a for a in peers if _tokens(_leaf(a["path"])) & _tokens(_leaf(gone["path"]))]
        if shared:
            pick, confidence = shared[0], "name-and-type"
        elif len(peers) == 1:
            pick, confidence = peers[0], "type-only"
        else:
            continue
        used.add(pick["path"])
        candidates.append(
            {
                "kind": "rename-candidate",
                "path": gone["path"],
                "severity": "BREAKING",
                "before": _leaf(gone["path"]),
                "after": _leaf(pick["path"]),
                "to_path": pick["path"],
                "type": gone["before"],
                "confidence": confidence,
            }
        )
    return candidates


def summarize_change(change: Change) -> str:
    path, kind = change["path"], change["kind"]
    if kind == "field-removed":
        return f"{path} was removed (was {change['before']})"
    if kind == "field-added":
        return f"{path} was added ({change['after']})"
    if kind == "type-changed":
        return f"{path}: {change['before']} -> {change['after']}"
    if kind == "became-nullable":
        return f"{path} can now be null (was {change['before']})"
    if kind == "became-non-nullable":
        return f"{path} is no longer nullable"
    if kind == "required-to-optional":
        return f"{path} is no longer always present"
    if kind == "optional-to-required":
        return f"{path} is now required"
    if kind == "rename-candidate":
        return f"{change['before']} -> {change['after']} (candidate rename at {path}, {change['type']})"
    if kind == "endpoint-changed":
        return f"{change['before']} -> {change['after']}"
    if kind == "endpoint-deprecated":
        return f"{path} is deprecated" + (f" (sunset {change['after']})" if change.get("after") else "")
    if kind == "endpoint-gone":
        return f"{path} now returns {change['after']}"
    if kind == "provider-switched":
        return f"provider changed: {change['before']} -> {change['after']}"
    if kind == "env-renamed":
        return f"environment variable {change['before']} -> {change['after']}"
    return f"{path}: {kind}"


def worst_severity(changes: list[Change]) -> str | None:
    for level in ("BREAKING", "WARNING", "INFO"):
        if any(c["severity"] == level for c in changes):
            return level
    return None
