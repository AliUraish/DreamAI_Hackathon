"""Deterministic hash of a schema tree (sorted keys, sorted union options).

Port of `@schema-watch/core` hash.ts (Apache-2.0). The hash is a cheap "did the
shape change at all" gate; a changed hash with an empty diff is not drift
(an empty array or a null-only field changes the hash but carries no signal).
"""

from __future__ import annotations

import hashlib

from .infer import SchemaNode


def hash_schema(node: SchemaNode) -> str:
    return hashlib.sha256(_canonicalize(node).encode()).hexdigest()


def _canonicalize(node: SchemaNode) -> str:
    n = 1 if node["nullable"] else 0
    kind = node["kind"]
    if kind == "unknown":
        return f"u{n}"
    if kind == "primitive":
        return f"p{n}:{node['type']}"
    if kind == "array":
        return f"a{n}:{_canonicalize(node['items']) if node['items'] else '-'}"
    if kind == "object":
        body = ",".join(
            f"{key}{'!' if prop['required'] else '?'}={_canonicalize(prop['schema'])}"
            for key, prop in sorted(node["properties"].items())
        )
        return f"o{n}:{{{body}}}"
    return f"U{n}:[{','.join(sorted(_canonicalize(o) for o in node['options']))}]"
