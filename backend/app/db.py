"""Storage. Postgres (Neon) when NEON_DB is set, SQLite otherwise.

Rows are dicts; JSON columns are (de)serialized here. SQL is written once with
`?` placeholders and translated for Postgres.
"""
# ruff: noqa: PLW0603

from __future__ import annotations

import json
import os
import queue
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from .config import settings

_JSON_COLUMNS = {
    "repos": {"validation_commands"},
    "integrations": {"endpoints", "baseline", "files"},
    "migrations": {"changes", "affected_files", "steps", "validation", "patched_files", "meta", "review"},
    "events": {"data"},
    "env_snapshots": {"entries"},
    "env_changes": {"details"},
    "agents": {"data"},
    "notifications": {"data"},
    "audits": {"findings", "metrics"},
}

_TABLES = {
    "workspaces": "id TEXT PRIMARY KEY, name TEXT NOT NULL, api_key_hash TEXT NOT NULL, api_key_prefix TEXT NOT NULL, salt TEXT, created_at TEXT",
    "repos": ("id TEXT PRIMARY KEY, name TEXT NOT NULL, full_name TEXT, local_path TEXT NOT NULL, remote_url TEXT, default_branch TEXT, "
              "validation_commands TEXT, connected_via TEXT, env_source TEXT, created_at TEXT"),
    "integrations": ("id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, provider TEXT NOT NULL, name TEXT NOT NULL, version TEXT, status TEXT NOT NULL, "
                     "docs_url TEXT, endpoints TEXT, baseline TEXT, files TEXT, last_checked_at TEXT, created_at TEXT"),
    "migrations": ("id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, integration_id TEXT NOT NULL, title TEXT, status TEXT NOT NULL, kind TEXT, "
                   "trigger TEXT, from_version TEXT, to_version TEXT, changes TEXT, affected_files TEXT, steps TEXT, validation TEXT, "
                   "patched_files TEXT, meta TEXT, summary TEXT, diff TEXT, branch TEXT, pr_url TEXT, pr_number INTEGER, pr_state TEXT, "
                   "merged_at TEXT, review TEXT, error TEXT, created_at TEXT, updated_at TEXT"),
    "events": "seq {serial}, id TEXT, repo_id TEXT, integration_id TEXT, migration_id TEXT, ts TEXT, type TEXT, message TEXT, data TEXT",
    "env_snapshots": "id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, entries TEXT, taken_at TEXT",
    "env_changes": ("id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, status TEXT NOT NULL, kind TEXT NOT NULL, summary TEXT, from_provider TEXT, "
                    "to_provider TEXT, details TEXT, migration_id TEXT, created_at TEXT, resolved_at TEXT"),
    # One private agent per repository: what it is doing now...
    "agents": "id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, status TEXT NOT NULL, task TEXT, detail TEXT, migration_id TEXT, data TEXT, started_at TEXT, updated_at TEXT",
    # ...and what it has learned about that repository. Never shared between repositories.
    "agent_memory": "id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT",
    "explanations": "id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, target TEXT NOT NULL, content_hash TEXT, text TEXT, source TEXT, created_at TEXT",
    # What each project's agent concluded on its periodic look at the whole pipeline. Not shown in the UI feed.
    "audits": "id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, verdict TEXT, source TEXT, window_seconds INTEGER, summary TEXT, findings TEXT, metrics TEXT, action TEXT, created_at TEXT",
    "notifications": "id TEXT PRIMARY KEY, repo_id TEXT, level TEXT, title TEXT, body TEXT, link TEXT, data TEXT, read_at TEXT, created_at TEXT",
}

# Reads are served from an in-memory mirror; writes update the mirror at once and are sent to the
# database in order by a background writer. A query to a hosted Postgres can cost a second from a
# laptop; a pipeline run stores ~80 rows and the UI polls every few seconds, so neither can wait on it.
# The database stays the durable store: the mirror is loaded from it at start-up. This assumes one
# backend process per database, which is how Chowkidaar is deployed.
_lock = threading.RLock()
_conn: Any = None
_dialect = "sqlite"
_mirror: dict[str, dict[Any, dict[str, Any]]] = {}
_next_seq = 1
_writes: "queue.Queue[tuple[str, list[Any]] | None]" = queue.Queue()
_writer: threading.Thread | None = None


def database_url() -> str | None:
    return os.environ.get("NEON_DB") or os.environ.get("DATABASE_URL") or None


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def dialect() -> str:
    connect()
    return _dialect


def _open() -> Any:
    global _dialect
    url = database_url()
    if url:
        import psycopg
        from psycopg.rows import dict_row
        _dialect = "postgres"
        # prepare_threshold=None: Neon's pooled endpoint is PgBouncer in transaction mode, which cannot keep prepared statements.
        return psycopg.connect(url, autocommit=True, row_factory=dict_row, prepare_threshold=None, connect_timeout=15)
    _dialect = "sqlite"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    legacy = settings.data_dir / ("patch" + "layer.db")  # the file's name before the product was renamed
    if legacy.exists() and not settings.db_path.exists():
        legacy.rename(settings.db_path)
    conn = sqlite3.connect(settings.db_path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(table: str) -> list[str]:
    return [c.strip().split()[0] for c in _TABLES[table].split(",")]


def _migrate(conn: Any) -> None:
    serial = "BIGSERIAL PRIMARY KEY" if _dialect == "postgres" else "INTEGER PRIMARY KEY AUTOINCREMENT"
    create = [f"CREATE TABLE IF NOT EXISTS {table} ({columns.format(serial=serial)})" for table, columns in _TABLES.items()]
    if _dialect == "postgres":  # one round trip for all tables, one for all columns
        conn.execute("; ".join(create))
        rows = conn.execute("SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = current_schema()").fetchall()
        existing = {(r["table_name"], r["column_name"]) for r in rows}
    else:
        existing = set()
        for statement, table in zip(create, _TABLES):
            conn.execute(statement)
            existing |= {(table, r["name"]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    # Columns introduced after a database was first created.
    alters = [f"ALTER TABLE {table} ADD COLUMN {column.strip()}" for table, columns in _TABLES.items() for column in columns.split(",")
              if (table, column.strip().split()[0]) not in existing and "PRIMARY KEY" not in column]
    for statement in alters if _dialect == "sqlite" else ([";".join(alters)] if alters else []):
        conn.execute(statement)


def _load(conn: Any) -> None:
    """Fill the mirror. One query for everything on Postgres (events: the most recent few thousand)."""
    global _next_seq
    _mirror.clear()
    for table in _TABLES:
        _mirror[table] = {}
    if _dialect == "postgres":
        parts = [f"(SELECT '{t}' AS t, to_jsonb(x)::text AS r FROM {t} x" + (" ORDER BY seq DESC LIMIT 4000)" if t == "events" else ")") for t in _TABLES]
        for row in conn.execute(" UNION ALL ".join(parts)).fetchall():
            data = json.loads(row["r"])
            _mirror[row["t"]][data["seq" if row["t"] == "events" else "id"]] = _decode(row["t"], data)
    else:
        for table in _TABLES:
            for row in conn.execute(f"SELECT * FROM {table}").fetchall():
                data = _decode(table, row)
                _mirror[table][data["seq" if table == "events" else "id"]] = data
    _next_seq = max(_mirror["events"], default=0) + 1


def connect() -> Any:
    global _conn, _writer
    with _lock:
        if _conn is None:
            _conn = _open()
            _migrate(_conn)
            _load(_conn)
            if _writer is None or not _writer.is_alive():
                _writer = threading.Thread(target=_write_forever, name="db-writer", daemon=True)
                _writer.start()
        return _conn


def flush(timeout: float = 120) -> None:
    """Wait until every queued write has reached the database."""
    deadline = time.monotonic() + timeout
    while _writes.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.02)


def reset_connection() -> None:
    """Flush, then drop the connection and the mirror (tests point the database somewhere else)."""
    global _conn
    flush()
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None
        _mirror.clear()


def _write_forever() -> None:
    global _conn
    while True:
        item = _writes.get()
        try:
            if item is None:
                continue
            sql, params = item
            for attempt in (1, 2):
                try:
                    with _lock:
                        conn = _conn
                    if conn is None:
                        break  # the connection was reset under us (tests); the mirror went with it
                    conn.execute(sql.replace("?", "%s") if _dialect == "postgres" else sql, params)
                    break
                except Exception as exc:
                    # Neon suspends idle computes and drops the connection: open a new one and retry once.
                    if attempt == 2 or _dialect != "postgres" or type(exc).__name__ not in {"OperationalError", "InterfaceError"}:
                        print(f"db write failed: {type(exc).__name__}: {str(exc).splitlines()[0][:200]}")
                        break
                    with _lock:
                        try:
                            _conn = _open()
                        except Exception as reopen:
                            print(f"db reconnect failed: {reopen}")
                            break
        finally:
            _writes.task_done()


def _decode(table: str, row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    for col in _JSON_COLUMNS.get(table, ()):
        if isinstance(out.get(col), str):
            out[col] = json.loads(out[col])
    return out


def _encode(table: str, values: dict[str, Any]) -> dict[str, Any]:
    json_cols = _JSON_COLUMNS.get(table, ())
    return {k: (json.dumps(v) if k in json_cols and v is not None else v) for k, v in values.items()}


def _copy(row: dict[str, Any] | None) -> dict[str, Any] | None:
    return None if row is None else json.loads(json.dumps(row))  # callers mutate what they get


def insert(table: str, values: dict[str, Any]) -> dict[str, Any]:
    global _next_seq
    connect()
    with _lock:
        stored = {c: None for c in _columns(table)} | json.loads(json.dumps(values))
        if table == "events":
            stored["seq"] = _next_seq
            _next_seq += 1
        _mirror[table][stored["seq" if table == "events" else "id"]] = stored
        encoded = _encode(table, {k: v for k, v in stored.items() if k in values or k == "seq" and table == "events"})
    cols, marks = ", ".join(encoded), ", ".join("?" for _ in encoded)
    _writes.put((f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(encoded.values())))
    return values


def update(table: str, row_id: str, values: dict[str, Any]) -> None:
    connect()
    with _lock:
        row = _mirror[table].get(row_id)
        if row is None:
            return
        row.update(json.loads(json.dumps(values)))
    encoded = _encode(table, values)
    sets = ", ".join(f"{k} = ?" for k in encoded)
    _writes.put((f"UPDATE {table} SET {sets} WHERE id = ?", [*encoded.values(), row_id]))


def get(table: str, row_id: str) -> dict[str, Any] | None:
    connect()
    with _lock:
        return _copy(_mirror[table].get(row_id))


def _sorted(rows: list[dict[str, Any]], order: str) -> list[dict[str, Any]]:
    """Timestamps have one-second resolution, so ties are common; they are broken by insertion order (the mirror keeps it)."""
    field, _, direction = order.partition(" ")
    indexed = list(enumerate(rows))
    indexed.sort(key=lambda item: (item[1].get(field) is None, item[1].get(field) if item[1].get(field) is not None else "", item[0]),
                 reverse=direction.upper() == "DESC")
    return [row for _, row in indexed]


def select(table: str, where: dict[str, Any] | None = None, order: str = "created_at DESC", limit: int = 500) -> list[dict[str, Any]]:
    connect()
    if "created_at" in order and "created_at" not in _TABLES[table]:
        order = "id ASC"
    with _lock:
        rows = [r for r in _mirror[table].values() if all(r.get(k) == v for k, v in (where or {}).items())]
        return [_copy(r) for r in _sorted(rows, order)[:limit]]  # type: ignore[misc]


def delete(table: str, where: dict[str, Any]) -> None:
    connect()
    with _lock:
        for key in [k for k, r in _mirror[table].items() if all(r.get(f) == v for f, v in where.items())]:
            del _mirror[table][key]
    clause = " AND ".join(f"{k} = ?" for k in where)
    _writes.put((f"DELETE FROM {table} WHERE {clause}", list(where.values())))


def events_after(seq: int, where: dict[str, Any] | None = None, limit: int = 500) -> list[dict[str, Any]]:
    connect()
    with _lock:
        rows = [r for s, r in _mirror["events"].items() if s > seq and all(r.get(k) == v for k, v in (where or {}).items())]
        return [_copy(r) for r in sorted(rows, key=lambda r: r["seq"])[:limit]]  # type: ignore[misc]
