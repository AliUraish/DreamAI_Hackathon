"""Notifications: the few events a person should be interrupted for."""

from __future__ import annotations

from typing import Any

from . import db, ui_events


def send(title: str, body: str = "", *, repo_id: str | None = None, level: str = "info", link: str | None = None,
         data: dict[str, Any] | None = None) -> dict[str, Any]:
    """level: info | success | warning | action (the user has to decide something)."""
    row = db.insert("notifications", {"id": db.new_id("ntf"), "repo_id": repo_id, "level": level, "title": title, "body": body,
                                      "link": link, "data": data or {}, "read_at": None, "created_at": db.now()})
    ui_events.broadcast({"t": "notify", "repoId": repo_id, "notification": public(row)})
    return row


def public(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "repoId": row["repo_id"], "level": row["level"], "title": row["title"], "body": row["body"],
            "link": row["link"], "read": row["read_at"] is not None, "createdAt": row["created_at"]}


def recent(limit: int = 40) -> list[dict[str, Any]]:
    return [public(r) for r in db.select("notifications", limit=limit)]


def mark_read(ids: list[str] | None = None) -> None:
    for row in db.select("notifications", limit=500):
        if row["read_at"] is None and (ids is None or row["id"] in ids):
            db.update("notifications", row["id"], {"read_at": db.now()})
