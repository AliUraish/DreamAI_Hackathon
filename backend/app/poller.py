"""Observed drift: call the live endpoint, infer the response shape, compare it
with the stored baseline. Also reads the standard deprecation signals
(Deprecation / Sunset headers, Link rel="successor-version", 404/410)."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

import httpx

from . import db
from .providers import get_provider, probe_headers
from .schema import detect_renames, diff_schemas, hash_schema, infer_schema

# On live third-party list endpoints, optionality flips with the data (one new
# voice without a field), so only these kinds count as drift there.
STRICT_KINDS = {"field-removed", "type-changed"}

_LINK_RE = re.compile(r'<([^>]+)>\s*;\s*rel="?([\w-]+)"?')


def parse_links(header: str | None) -> dict[str, str]:
    return {rel: target for target, rel in _LINK_RE.findall(header or "")}


def sample(method: str, url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    try:
        response = httpx.request(method, url, headers=headers or {}, timeout=15, follow_redirects=True)
    except httpx.HTTPError as exc:
        return {"ok": False, "status": None, "error": str(exc), "links": {}, "deprecated": False, "sunset": None, "body": None}
    body = None
    if "json" in response.headers.get("content-type", ""):
        try:
            body = response.json()
        except ValueError:
            body = None
    links = parse_links(response.headers.get("link"))
    if isinstance(body, dict):  # a 410 body may carry the same pointers
        links.setdefault("successor-version", body.get("successor") or "")
        links.setdefault("deprecation", body.get("migration_guide") or "")
    links = {rel: urljoin(url, target) for rel, target in links.items() if target}
    return {"ok": response.is_success, "status": response.status_code, "error": None, "links": links,
            "deprecated": response.headers.get("deprecation") is not None, "sunset": response.headers.get("sunset"), "body": body}


def snapshot(body: Any) -> dict[str, Any]:
    schema = infer_schema(body)
    return {"schema": schema, "hash": hash_schema(schema), "sampled_at": db.now()}


def check_endpoint(endpoint: dict[str, Any], baseline: dict[str, Any] | None, *, strict: bool = False,
                   successor_url: str | None = None) -> dict[str, Any]:
    """One probe. Returns {changes, renames, baseline, successor_url, docs_url, status, skipped}."""
    provider = get_provider(endpoint.get("provider", "")) or {}
    probe = next((p for p in provider.get("probes", []) if p["id"] == endpoint["id"]), endpoint)
    headers = probe_headers(probe)
    if headers is None:
        return {"skipped": f"{probe['auth']['env']} is not set", "changes": [], "renames": [], "baseline": baseline}

    result = sample(endpoint["method"], endpoint["url"], headers)
    changes: list[dict[str, Any]] = []
    path = endpoint.get("path") or endpoint["url"]
    successor_url = successor_url or result["links"].get("successor-version")
    docs_url = result["links"].get("deprecation")

    if result["status"] is None:
        return {"skipped": f"unreachable: {result['error']}", "changes": [], "renames": [], "baseline": baseline}

    compare_body = None
    if result["status"] in (404, 410):
        changes.append({"kind": "endpoint-gone", "path": path, "severity": "BREAKING", "before": "200", "after": str(result["status"])})
    elif result["deprecated"] or (successor_url and successor_url != endpoint["url"]):
        changes.append({"kind": "endpoint-deprecated", "path": path, "severity": "WARNING", "before": None, "after": result["sunset"]})
    if result["ok"]:
        compare_body = result["body"]

    # When the provider points at a successor, the contract to migrate to is the successor's.
    if successor_url and successor_url != endpoint["url"]:
        successor = sample(endpoint["method"], successor_url, headers)
        if successor["ok"]:
            compare_body = successor["body"]
            new_path = "/" + successor_url.split("://", 1)[-1].split("/", 1)[-1]
            changes.append({"kind": "endpoint-changed", "path": path, "severity": "BREAKING", "before": path, "after": new_path})
            docs_url = docs_url or successor["links"].get("deprecation")

    new_baseline = baseline
    if compare_body is not None:
        current = snapshot(compare_body)
        if baseline is None:
            new_baseline = current  # first sight: record, nothing to compare
        elif current["hash"] != baseline["hash"]:
            field_changes = diff_schemas(baseline["schema"], current["schema"], "response")
            if strict:
                field_changes = [c for c in field_changes if c["kind"] in STRICT_KINDS]
            changes.extend(field_changes)
            if not field_changes and not changes:
                new_baseline = current  # hash moved, shape did not (empty array, null-only field)

    return {"skipped": None, "status": result["status"], "changes": changes, "renames": detect_renames(changes),
            "baseline": new_baseline, "successor_url": successor_url, "docs_url": docs_url}
