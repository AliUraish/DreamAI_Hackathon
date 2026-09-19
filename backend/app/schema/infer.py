"""Structural shape inference for JSON values: type, nullability, nesting - never the data.

Python port of `@schema-watch/core` (packages/core/src/infer.ts), Apache-2.0,
https://github.com/HenryMorganDibie/schema-watch. See NOTICE.

A schema node is a plain dict so it can be stored and served as JSON:

    {"kind": "primitive", "type": "string"|"number"|"boolean", "nullable": bool}
    {"kind": "array",  "items": node | None, "nullable": bool}
    {"kind": "object", "properties": {key: {"required": bool, "schema": node}}, "nullable": bool}
    {"kind": "union",  "options": [node, ...], "nullable": bool}
    {"kind": "unknown", "nullable": bool}      # only ever seen as null
"""

from __future__ import annotations

from functools import reduce
from typing import Any

SchemaNode = dict[str, Any]


def infer_schema(value: Any) -> SchemaNode:
    if value is None:
        return {"kind": "unknown", "nullable": True}
    if isinstance(value, bool):  # before int: bool is an int in Python
        return {"kind": "primitive", "type": "boolean", "nullable": False}
    if isinstance(value, (int, float)):
        return {"kind": "primitive", "type": "number", "nullable": False}
    if isinstance(value, str):
        return {"kind": "primitive", "type": "string", "nullable": False}
    if isinstance(value, list):
        if not value:
            return {"kind": "array", "items": None, "nullable": False}
        return {"kind": "array", "items": reduce(merge_schema, map(infer_schema, value)), "nullable": False}
    if isinstance(value, dict):
        properties = {str(k): {"required": True, "schema": infer_schema(v)} for k, v in value.items()}
        return {"kind": "object", "properties": properties, "nullable": False}
    raise TypeError(f"not a JSON value: {type(value).__name__}")


def merge_schema(a: SchemaNode, b: SchemaNode) -> SchemaNode:
    """Merge two nodes seen at the same position into one that describes both.

    Divergent shapes become a union rather than being collapsed, so the differ
    can still tell "string" from "string | number".
    """
    nullable = a["nullable"] or b["nullable"]
    if a["kind"] == "unknown" and b["kind"] == "unknown":
        return {"kind": "unknown", "nullable": nullable}
    if a["kind"] == "unknown":
        return {**b, "nullable": nullable}
    if b["kind"] == "unknown":
        return {**a, "nullable": nullable}

    if a["kind"] == "primitive" and b["kind"] == "primitive":
        if a["type"] == b["type"]:
            return {"kind": "primitive", "type": a["type"], "nullable": nullable}
        return _union_of([{**a, "nullable": False}, {**b, "nullable": False}], nullable)

    if a["kind"] == "array" and b["kind"] == "array":
        if a["items"] and b["items"]:
            items = merge_schema(a["items"], b["items"])
        else:
            items = a["items"] or b["items"]
        return {"kind": "array", "items": items, "nullable": nullable}

    if a["kind"] == "object" and b["kind"] == "object":
        properties: dict[str, Any] = {}
        for key in {*a["properties"], *b["properties"]}:
            ap, bp = a["properties"].get(key), b["properties"].get(key)
            if ap and bp:
                properties[key] = {
                    "required": ap["required"] and bp["required"],
                    "schema": merge_schema(ap["schema"], bp["schema"]),
                }
            else:
                properties[key] = {"required": False, "schema": (ap or bp)["schema"]}
        return {"kind": "object", "properties": properties, "nullable": nullable}

    a_options = a["options"] if a["kind"] == "union" else [{**a, "nullable": False}]
    b_options = b["options"] if b["kind"] == "union" else [{**b, "nullable": False}]
    return _union_of([*a_options, *b_options], nullable)


def _signature(node: SchemaNode) -> str:
    kind = node["kind"]
    if kind == "unknown":
        return "unknown"
    if kind == "primitive":
        return node["type"]
    if kind == "array":
        return f"array<{_signature(node['items']) if node['items'] else 'unknown'}>"
    if kind == "object":
        return "object:" + ",".join(sorted(node["properties"]))
    return "|".join(sorted(_signature(o) for o in node["options"]))


def _union_of(options: list[SchemaNode], nullable: bool) -> SchemaNode:
    seen: dict[str, SchemaNode] = {}
    for option in options:
        seen.setdefault(_signature(option), option)
    deduped = list(seen.values())
    if len(deduped) == 1:
        return {**deduped[0], "nullable": nullable}
    return {"kind": "union", "options": deduped, "nullable": nullable}
