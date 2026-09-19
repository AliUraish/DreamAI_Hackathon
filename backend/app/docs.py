"""Fetches (and caches) the provider's migration documentation."""

from __future__ import annotations

import hashlib
import re

import httpx

from .config import settings

MAX_DOC_CHARS = 60_000


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


def fetch_docs(url: str | None, *, refresh: bool = False) -> dict[str, str | None]:
    if not url:
        return {"url": None, "text": None, "error": "no documentation URL known"}
    cache = settings.data_dir / "docs" / (hashlib.sha256(url.encode()).hexdigest()[:16] + ".md")
    if cache.exists() and not refresh:
        return {"url": url, "text": cache.read_text(), "error": None}
    try:
        response = httpx.get(url, timeout=20, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return {"url": url, "text": None, "error": str(exc).splitlines()[0]}
    text = response.text
    if "html" in response.headers.get("content-type", ""):
        text = _strip_html(text)
    truncated = len(text) > MAX_DOC_CHARS  # reported in the activity feed, never silent
    text = text[:MAX_DOC_CHARS]
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(text)
    return {"url": url, "text": text, "error": None, "truncated": truncated}
