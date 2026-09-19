"""Finds where a repository talks to external APIs, using ast-grep.

This is the *seed* step of the integration map: the exact call sites (file,
line, enclosing function, URL). What depends on those call sites is the graph's
job (graph.py), not this module's.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ast_grep_py import SgNode, SgRoot

from .providers import match_provider

LANGUAGES = {".ts": "typescript", ".tsx": "tsx", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".py": "python"}
IGNORED_DIRS = {"node_modules", ".git", "dist", "build", ".next", "coverage", "graphify-out", ".venv", "venv", "__pycache__"}
TEST_MARKERS = ("/tests/", "/test/", "/__tests__/", ".test.", ".spec.", "test_")

# ($URL is the first argument; $$$ swallows the rest.)
HTTP_CALL_PATTERNS = {
    "typescript": ["fetch($URL)", "fetch($URL, $$$A)", "axios.$M($URL)", "axios.$M($URL, $$$A)", "axios($URL)", "got($URL, $$$A)", "got.$M($URL, $$$A)", "ky.$M($URL, $$$A)"],
    "python": ["requests.$M($URL)", "requests.$M($URL, $$$A)", "httpx.$M($URL)", "httpx.$M($URL, $$$A)", "$C.get($URL, $$$A)", "$C.post($URL, $$$A)"],
}
HTTP_CALL_PATTERNS["tsx"] = HTTP_CALL_PATTERNS["javascript"] = HTTP_CALL_PATTERNS["typescript"]

FUNCTION_KINDS = {"function_declaration", "method_definition", "arrow_function", "function_expression", "function_definition"}
_PATH_RE = re.compile(r"(/(?:v\d+|api)[A-Za-z0-9_\-/{}$.:]*)")
_ENV_RE = re.compile(r"(?:process\.env\.|os\.environ(?:\.get)?\W+|getenv\W+)([A-Z][A-Z0-9_]+)")
_IMPORT_RE = re.compile(r"""(?:from\s+['"]([^'"./][^'"]*)['"]|require\(\s*['"]([^'"./][^'"]*)['"]\s*\)|^\s*(?:import|from)\s+([a-zA-Z_][\w.]*))""", re.M)


@dataclass
class CallSite:
    file: str
    line: int
    function: str | None
    url: str
    path: str | None
    method: str
    provider: str | None = None
    is_test: bool = False


@dataclass
class ProviderUsage:
    provider: str
    name: str
    call_sites: list[CallSite] = field(default_factory=list)
    sdk_files: list[str] = field(default_factory=list)

    @property
    def files(self) -> list[str]:
        return sorted({c.file for c in self.call_sites if not c.is_test} | set(self.sdk_files))

    def to_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "name": self.name, "files": self.files,
                "call_sites": [asdict(c) for c in self.call_sites], "sdk_files": self.sdk_files}


def iter_source_files(root: Path):
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in LANGUAGES and not (set(path.relative_to(root).parts) & IGNORED_DIRS):
            yield path


def _enclosing_function(node: SgNode) -> str | None:
    for ancestor in node.ancestors():
        if ancestor.kind() in FUNCTION_KINDS:
            name = ancestor.field("name")
            if name:
                return name.text()
            parent = ancestor.parent()  # const foo = async () => ...
            if parent and parent.kind() == "variable_declarator" and parent.field("name"):
                return parent.field("name").text()
    return None


def _http_method(match: SgNode, text: str) -> str:
    verb = match.get_match("M")
    if verb and verb.text().lower() in {"get", "post", "put", "patch", "delete"}:
        return verb.text().upper()
    found = re.search(r"method\s*[:=]\s*['\"](\w+)['\"]", text)
    return found.group(1).upper() if found else "GET"


def _resolve_identifiers(url_text: str, source: str) -> str:
    """Inline simple `const BASE = ...` definitions so the URL text carries its env var / host."""
    resolved = url_text
    for ident in set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", url_text)):
        definition = re.search(rf"(?:const|let|var)?\s*\b{re.escape(ident)}\b\s*(?::[^=]+)?=\s*([^;\n]+)", source)
        if definition:
            resolved += " " + definition.group(1)
    return resolved


def scan_repo(root: Path) -> dict[str, ProviderUsage]:
    """provider id -> usage. Call sites that match no known provider are dropped."""
    usages: dict[str, ProviderUsage] = {}

    def usage_for(provider: dict[str, Any]) -> ProviderUsage:
        return usages.setdefault(provider["id"], ProviderUsage(provider["id"], provider["name"]))

    for path in iter_source_files(root):
        rel = path.relative_to(root).as_posix()
        source = path.read_text(encoding="utf-8", errors="ignore")
        language = LANGUAGES[path.suffix]
        is_test = any(marker in f"/{rel}" for marker in TEST_MARKERS)
        packages = {(a or b or c).split("/")[0] if not (a or b or c).startswith("@") else "/".join((a or b or c).split("/")[:2])
                    for a, b, c in _IMPORT_RE.findall(source)}

        try:
            tree = SgRoot(source, language).root()
        except Exception:
            continue

        seen: set[tuple[int, int]] = set()
        for pattern in HTTP_CALL_PATTERNS.get(language, []):
            for match in tree.find_all(pattern=pattern):
                rng = match.range()
                if (rng.start.line, rng.start.column) in seen:
                    continue
                seen.add((rng.start.line, rng.start.column))
                url_node = match.get_match("URL")
                if not url_node:
                    continue
                url_text = url_node.text()
                context = _resolve_identifiers(url_text, source)
                provider = match_provider(context, set(_ENV_RE.findall(context)), set())
                if not provider:
                    continue
                api_path = _PATH_RE.search(url_text)
                usage_for(provider).call_sites.append(CallSite(
                    file=rel, line=rng.start.line + 1, function=_enclosing_function(match), url=url_text.strip("`'\""),
                    path=api_path.group(1) if api_path else None, method=_http_method(match, match.text()),
                    provider=provider["id"], is_test=is_test))

        if not is_test:
            sdk = match_provider("", set(), packages)
            if sdk:
                usage = usage_for(sdk)
                if rel not in usage.sdk_files:
                    usage.sdk_files.append(rel)
    return usages
