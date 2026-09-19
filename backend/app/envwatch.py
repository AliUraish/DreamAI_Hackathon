"""Senses provider changes from a repository's environment variables.

A credential's *name* says which provider it belongs to (`ANTHROPIC_API_KEY`,
`AWS_OPENAI_KEY` = OpenAI reached through AWS) and the *hash of its value* says
whether it is still the same credential. When a name disappears, another appears
and the value hash is different, the project has moved to a different provider -
and the code that still calls the old one has to follow.

Chowkidaar never acts on that inference alone. It records a pending question
(`env_changes`, status "pending"); the UI asks the user; only a confirmation
starts the migration pipeline.

Secret handling: values are read only to (1) compute a keyed HMAC and (2) look at
a well-known key prefix. They are never stored, logged, returned by the API or
sent to the LLM. The HMAC key is random per installation, so a fingerprint is
useless outside this machine.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from . import db, events
from .config import settings
from .providers import all_providers, get_provider
from .scanner import ProviderUsage, iter_source_files

# Highest precedence first: the first file that gives a name a real value wins.
ENV_FILES = [".env.local", ".env", ".env.development", ".env.production", ".env.example", ".env.sample", ".env.template"]
EXAMPLE_FILES = {".env.example", ".env.sample", ".env.template"}

PLATFORM_TOKENS = {"aws": ["AWS", "BEDROCK"], "azure": ["AZURE"], "gcp": ["VERTEX", "GCP", "GOOGLE_CLOUD"]}
PLATFORM_KEY_PREFIXES = {"aws": ["AKIA", "ASIA", "ABSK"]}
_CREDENTIAL_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|SID|CREDENTIAL|AUTH|_URL|ENDPOINT|BASE_URL|HOST)")
_PLACEHOLDER_RE = re.compile(r"^(|x+|\.+|<.*>|\$\{.*\}|your[-_ ].*|.*your[-_]?(api[-_]?)?key.*|changeme|change[-_]me|todo|replace[-_]?me|placeholder|example|null|none|sk-\.\.\.)$", re.I)
# process.env.X, import.meta.env.X, os.environ["X"], getenv("X"), and runtime bindings such as Cloudflare Workers' `c.env.X` / `env.X`
_CODE_ENV_RE = re.compile(r"""(?:process\.env\.|process\.env\[\s*['"]|import\.meta\.env\.|os\.environ(?:\.get)?[\[(]\s*['"]|getenv\(\s*['"]|Deno\.env\.get\(\s*['"]|\benv\.)([A-Z][A-Z0-9_]{2,})""")


def _hmac_key() -> bytes:
    path = settings.data_dir / "env_hmac.key"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(secrets.token_bytes(32))
        path.chmod(0o600)
    return path.read_bytes()


def fingerprint(value: str) -> str:
    """Keyed hash of a secret value. 16 hex chars: enough to notice a change, not enough to be the secret."""
    return hmac.new(_hmac_key(), value.encode(), hashlib.sha256).hexdigest()[:16]


def is_placeholder(value: str | None) -> bool:
    return value is None or bool(_PLACEHOLDER_RE.match(value.strip()))


_SECRET_NAME_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)")


def holds_secret(name: str, value: str | None) -> bool:
    """True when a line of an env file carries something that must not leave the machine.
    A URL default in `.env.example` is fine; a filled-in key is not."""
    if is_placeholder(value):
        return False
    known_prefix = any(value.startswith(prefix) for provider in all_providers() for prefix in provider.get("key_prefixes", []))
    return known_prefix or bool(_SECRET_NAME_RE.search(name.upper()))


def classify(name: str, value: str | None = None) -> dict[str, Any]:
    """{provider, platform, by}: which provider a variable belongs to, and how we know.

    The name wins over the value: `AWS_OPENAI_KEY` is OpenAI (via AWS) whatever the key looks like."""
    upper = name.upper()
    platform = next((p for p, tokens in PLATFORM_TOKENS.items() if any(re.search(rf"(^|_){t}(_|$)", upper) for t in tokens)), None)
    for provider in all_providers():
        if upper in provider["detect"]["env"]:
            return {"provider": provider["id"], "platform": platform, "by": "name"}
    for provider in all_providers():
        if any(token in upper for token in provider.get("tokens", [])):
            return {"provider": provider["id"], "platform": platform, "by": "name"}
    if value and not is_placeholder(value):
        for provider in all_providers():
            if any(value.startswith(prefix) for prefix in provider.get("key_prefixes", [])):
                return {"provider": provider["id"], "platform": platform, "by": "value-prefix"}
        if platform is None:
            platform = next((p for p, prefixes in PLATFORM_KEY_PREFIXES.items() if any(value.startswith(x) for x in prefixes)), None)
    return {"provider": None, "platform": platform, "by": None}


def code_references(root: Path) -> dict[str, list[str]]:
    """env var name -> source files that read it."""
    refs: dict[str, list[str]] = {}
    for path in iter_source_files(root):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for name in set(_CODE_ENV_RE.findall(text)):
            refs.setdefault(name, []).append(path.relative_to(root).as_posix())
    return refs


def take_snapshot(root: Path) -> dict[str, dict[str, Any]]:
    """name -> {provider, platform, by, fingerprint, files, referenced_in}. No values."""
    entries: dict[str, dict[str, Any]] = {}
    for filename in ENV_FILES:
        path = root / filename
        if not path.is_file():
            continue
        for name, value in dotenv_values(path, interpolate=False).items():
            entry = entries.setdefault(name, {"name": name, "fingerprint": None, "files": [], "referenced_in": [], **classify(name, None)})
            entry["files"].append(filename)
            if entry["fingerprint"] is None and not is_placeholder(value):
                entry["fingerprint"] = fingerprint(value)  # the value goes no further than this line and classify()
                if entry["provider"] is None:
                    entry.update(classify(name, value))
    for name, files in code_references(root).items():
        entry = entries.setdefault(name, {"name": name, "fingerprint": None, "files": [], "referenced_in": [], **classify(name, None)})
        entry["referenced_in"] = sorted(files)
    return {name: e for name, e in entries.items() if e["provider"] or e["platform"] or _CREDENTIAL_RE.search(name)}


def snapshot_from_connector(root: Path, pushed: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The same snapshot shape, built from what the connector sent: names, fingerprints computed on the
    user's machine, and an optional provider hint from the key's prefix. The server adds which files read each variable."""
    refs = code_references(root)
    entries: dict[str, dict[str, Any]] = {}
    for item in pushed:
        name = str(item["name"])
        info = classify(name, None)
        if info["provider"] is None and item.get("hint"):
            info = {"provider": item["hint"], "platform": info["platform"], "by": "value-prefix"}
        entries[name] = {"name": name, "fingerprint": item.get("fingerprint"), "files": list(item.get("files") or []),
                         "referenced_in": sorted(refs.get(name, [])), **info}
    for name, files in refs.items():
        entries.setdefault(name, {"name": name, "fingerprint": None, "files": [], "referenced_in": sorted(files), **classify(name, None)})
    return {name: e for name, e in entries.items() if e["provider"] or e["platform"] or _CREDENTIAL_RE.search(name)}


def _category(provider_id: str | None) -> str | None:
    return (get_provider(provider_id) or {}).get("category") if provider_id else None


def _label(provider_id: str | None, platform: str | None) -> str:
    name = (get_provider(provider_id) or {}).get("name") if provider_id else None
    via = {"aws": "AWS", "azure": "Azure", "gcp": "Google Cloud"}.get(platform or "")
    return f"{name or 'an unrecognised provider'}{f' via {via}' if via else ''}"


def _active(snapshot: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The variables the project actually runs with. A real env file decides; `.env.example` often
    lags behind (people edit `.env` and forget it), so it only counts when there is no real file at
    all, as in a fresh clone."""
    real = {n: e for n, e in snapshot.items() if any(f not in EXAMPLE_FILES for f in e["files"])}
    return real or {n: e for n, e in snapshot.items() if e["files"]}


def diff_snapshots(before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Changes between two snapshots, as proposals for the user.

    kinds: provider-switched · env-renamed · provider-added · credential-removed · credential-rotated
    Only the first two need code to change; they carry `needs_confirmation: True`."""
    old, new = _active(before), _active(after)
    removed = [old[n] for n in old if n not in new]
    added = [new[n] for n in new if n not in old]
    proposals: list[dict[str, Any]] = []

    # Pair a removed credential with an added one: same kind of service first, else the only candidates left.
    pairs, unpaired_added = [], list(added)
    for gone in removed:
        same_category = [a for a in unpaired_added if _category(a["provider"]) and _category(a["provider"]) == _category(gone["provider"])]
        unknown = [a for a in unpaired_added if a["provider"] is None]
        pick = same_category[0] if same_category else (unknown[0] if unknown and gone["provider"] else None)
        if pick is None and len(removed) == 1 and len(unpaired_added) == 1:
            pick = unpaired_added[0]
        if pick:
            unpaired_added.remove(pick)
            pairs.append((gone, pick))

    for gone, new_entry in pairs:
        value_changed = gone["fingerprint"] != new_entry["fingerprint"]
        same_target = gone["provider"] == new_entry["provider"] and gone["platform"] == new_entry["platform"]
        if same_target or (not value_changed and new_entry["provider"] is None):
            kind = "env-renamed"
            summary = f"{gone['name']} was renamed to {new_entry['name']}" + ("" if value_changed else " (same credential)")
        else:
            kind = "provider-switched"
            summary = (f"{gone['name']} was replaced by {new_entry['name']} with a different credential: "
                       f"{_label(gone['provider'], gone['platform'])} → {_label(new_entry['provider'], new_entry['platform'])}")
        # Same credential under a new name: only references change, so the agent just does it.
        # A different provider (or one we cannot recognise) changes behaviour: that is the user's decision.
        automatic = kind == "env-renamed" and not value_changed
        proposals.append({"kind": kind, "summary": summary, "needs_confirmation": True, "automatic": automatic,
                          "from_provider": gone["provider"], "to_provider": new_entry["provider"],
                          "details": {"from_env": gone["name"], "to_env": new_entry["name"], "from_platform": gone["platform"],
                                      "to_platform": new_entry["platform"], "value_changed": value_changed,
                                      "classified_by": new_entry["by"], "referenced_in": gone["referenced_in"]}})

    paired_removed = {g["name"] for g, _ in pairs}
    for gone in removed:
        if gone["name"] not in paired_removed and gone["provider"]:
            proposals.append({"kind": "credential-removed", "needs_confirmation": False, "from_provider": gone["provider"], "to_provider": None,
                              "summary": f"{gone['name']} was removed; {len(gone['referenced_in'])} file(s) still read it",
                              "details": {"from_env": gone["name"], "referenced_in": gone["referenced_in"]}})
    for new_entry in unpaired_added:
        if new_entry["provider"]:
            proposals.append({"kind": "provider-added", "needs_confirmation": False, "from_provider": None, "to_provider": new_entry["provider"],
                              "summary": f"{new_entry['name']} was added: {_label(new_entry['provider'], new_entry['platform'])}",
                              "details": {"to_env": new_entry["name"], "to_platform": new_entry["platform"]}})

    # Same name, different value. A key rotation - unless the new key plainly belongs to someone else.
    for name in old.keys() & new.keys():
        a, b = old[name], new[name]
        if a["fingerprint"] and b["fingerprint"] and a["fingerprint"] != b["fingerprint"]:
            if a["provider"] and b["provider"] and a["provider"] != b["provider"]:
                proposals.append({"kind": "provider-switched", "needs_confirmation": True, "from_provider": a["provider"], "to_provider": b["provider"],
                                  "summary": f"{name} now holds a {_label(b['provider'], b['platform'])} credential (was {_label(a['provider'], a['platform'])})",
                                  "details": {"from_env": name, "to_env": name, "value_changed": True, "classified_by": b["by"], "referenced_in": b["referenced_in"]}})
            else:
                proposals.append({"kind": "credential-rotated", "needs_confirmation": False, "from_provider": a["provider"], "to_provider": b["provider"],
                                  "summary": f"{name} has a new value (same provider); no code change needed", "details": {"from_env": name, "to_env": name}})
    return proposals


# --- persistence + the question for the user -------------------------------------


def public_view(snapshot: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """What the API may show: names, providers, a short fingerprint. Never values."""
    return [{"name": e["name"], "provider": e["provider"], "platform": e["platform"], "classified_by": e["by"],
             "has_value": e["fingerprint"] is not None, "fingerprint": (e["fingerprint"] or "")[:8] or None,
             "files": e["files"], "referenced_in": e["referenced_in"]} for e in sorted(snapshot.values(), key=lambda e: e["name"])]


_notes: dict[str, list[dict[str, Any]]] = {}


def pop_notes(repo_id: str) -> list[dict[str, Any]]:
    """Changes from the last check that needed no answer (a provider's key added, a rotation, a removal)."""
    return _notes.pop(repo_id, [])


def check_repo(repo: dict[str, Any], pushed: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Compare the repo's environment with the last snapshot. Returns newly created env_changes rows.

    `pushed` is a snapshot sent by the connector; without it the env files are read from disk,
    which only happens for repositories that were connected by path and have no connector."""
    if pushed is None and repo.get("env_source") == "connector":
        return []
    root = Path(repo["local_path"])
    current = snapshot_from_connector(root, pushed) if pushed is not None else take_snapshot(root)
    stored = db.get("env_snapshots", repo["id"])
    if stored is None:
        db.insert("env_snapshots", {"id": repo["id"], "repo_id": repo["id"], "entries": current, "taken_at": db.now()})
        return []
    proposals = diff_snapshots(stored["entries"], current)
    db.update("env_snapshots", repo["id"], {"entries": current, "taken_at": db.now()})
    created = []
    for proposal in proposals:
        if not proposal["needs_confirmation"]:
            _notes.setdefault(repo["id"], []).append(proposal)
            events.emit(f"env.{proposal['kind']}", proposal["summary"], repo_id=repo["id"], data=proposal["details"])
            continue
        # A newer edit supersedes a question nobody answered yet about the same variable.
        for pending in db.select("env_changes", {"repo_id": repo["id"], "status": "pending"}):
            if pending["details"].get("to_env") == proposal["details"]["from_env"] or pending["details"].get("from_env") == proposal["details"]["from_env"]:
                db.update("env_changes", pending["id"], {"status": "superseded", "resolved_at": db.now()})
        row = db.insert("env_changes", {"id": db.new_id("env"), "repo_id": repo["id"], "status": "pending", "kind": proposal["kind"],
                                        "summary": proposal["summary"], "from_provider": proposal["from_provider"], "to_provider": proposal["to_provider"],
                                        "details": {**proposal["details"], "automatic": proposal.get("automatic", False)},
                                        "migration_id": None, "created_at": db.now(), "resolved_at": None})
        events.emit("env.change.detected", f"Environment change detected: {proposal['summary']}."
                    + (" Safe to handle; starting." if proposal.get("automatic") else " Waiting for confirmation."),
                    repo_id=repo["id"], data={"env_change_id": row["id"], "kind": row["kind"]})
        created.append(row)
    return created


def question_for(change: dict[str, Any]) -> dict[str, Any]:
    """The env_change as the UI should ask it."""
    details = change["details"]
    known_target = change["to_provider"] is not None
    if change["kind"] == "provider-switched":
        title = f"Switch from {_label(change['from_provider'], details.get('from_platform'))} to {_label(change['to_provider'], details.get('to_platform'))}?"
        body = (f"{details['from_env']} is gone and {details['to_env']} appeared with a different credential. "
                f"{len(details.get('referenced_in', []))} file(s) still use the old provider. "
                "Confirm and Chowkidaar will migrate that code, run your checks and open a pull request.")
    else:
        title = f"Update code to read {details['to_env']} instead of {details['from_env']}?"
        body = f"{len(details.get('referenced_in', []))} file(s) still read {details['from_env']}."
    return {**change, "question": {"title": title, "body": body, "needs_provider_choice": not known_target,
                                   "provider_options": [{"id": p["id"], "name": p["name"]} for p in all_providers()
                                                        if not change["from_provider"] or p.get("category") == _category(change["from_provider"])],
                                   "actions": {"confirm": f"/api/env-changes/{change['id']}/confirm", "dismiss": f"/api/env-changes/{change['id']}/dismiss"}}}


def usage_for_change(root: Path, usages: dict[str, ProviderUsage], change: dict[str, Any]) -> ProviderUsage:
    """The code that has to move: the old provider's call sites and SDK imports, plus every
    file that reads the old variable. Shaped as a ProviderUsage so the graph, the trace and
    the repair step treat it like any other integration."""
    old = usages.get(change["from_provider"] or "")
    provider = get_provider(change["from_provider"] or "") or {}
    usage = ProviderUsage(change["from_provider"] or "env", provider.get("name") or change["details"]["from_env"],
                          call_sites=list(old.call_sites) if old else [], sdk_files=list(old.sdk_files) if old else [])
    old_name = change["details"]["from_env"]
    pattern = re.compile(rf"\b{re.escape(old_name)}\b")
    for path in iter_source_files(root):
        rel = path.relative_to(root).as_posix()
        if rel not in usage.sdk_files and pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
            usage.sdk_files.append(rel)
    # Example env files document the variable. They go to the model only if no line in them holds a secret.
    for filename in EXAMPLE_FILES:
        path = root / filename
        if path.is_file() and old_name in path.read_text() and not any(holds_secret(n, v) for n, v in dotenv_values(path, interpolate=False).items()):
            usage.sdk_files.append(filename)
    return usage
