"""Workspace and API keys. The key is shown once at onboarding; only its SHA-256 is stored."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any

from . import db
from .config import settings

KEY_PREFIX = "ck_live_"


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def current() -> dict[str, Any] | None:
    rows = db.select("workspaces", order="created_at ASC", limit=1)
    return rows[0] if rows else None


def _new_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(24)


def create(name: str) -> tuple[dict[str, Any], str]:
    if current():
        raise ValueError("this installation is already set up; rotate the key instead")
    key = _new_key()
    workspace = db.insert("workspaces", {"id": db.new_id("ws"), "name": name.strip() or "My workspace", "api_key_hash": _hash(key),
                                         "api_key_prefix": key[:14], "salt": secrets.token_hex(32), "created_at": db.now()})
    return workspace, key


def rotate() -> str:
    workspace = current()
    if workspace is None:
        raise ValueError("finish onboarding first")
    key = _new_key()
    db.update("workspaces", workspace["id"], {"api_key_hash": _hash(key), "api_key_prefix": key[:14]})
    return key


def fingerprint_salt() -> str:
    """Keys the fingerprints a connector computes. Per workspace and stable, so rotating the API key does not make
    every credential look changed. It is not a secret from this server; it only makes fingerprints useless elsewhere."""
    workspace = current()
    if workspace is None:
        raise ValueError("finish onboarding first")
    if not workspace.get("salt"):
        workspace["salt"] = secrets.token_hex(32)
        db.update("workspaces", workspace["id"], {"salt": workspace["salt"]})
    return workspace["salt"]


def verify(key: str | None) -> bool:
    workspace = current()
    return bool(workspace and key and hmac.compare_digest(_hash(key), workspace["api_key_hash"]))


def connect_command(key: str = "<your API key>") -> str:
    """What the user runs inside their project. Hashing happens there; values never leave their machine."""
    base = settings.public_url.rstrip("/")
    return f"curl -fsSL {base}/api/v1/connector.py | CHOWKIDAAR_API_KEY={key} python3 - --watch"


def public(workspace: dict[str, Any] | None) -> dict[str, Any]:
    if workspace is None:
        return {"onboarded": False}
    return {"onboarded": True, "id": workspace["id"], "name": workspace["name"], "key_prefix": workspace["api_key_prefix"] + "…",
            "created_at": workspace["created_at"], "connect_command": connect_command()}
