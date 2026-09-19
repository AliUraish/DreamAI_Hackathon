"""Run the suite against Postgres with CHOWKIDAAR_TEST_PG=postgresql://... ; otherwise each test gets its own SQLite file."""

import os

import pytest

# Importing the config loads backend/.env into the environment. Do that first, then strip every real
# credential for the whole session: a test must never reach the developer's database, LLM key or GitHub.
import app.config  # noqa: F401,E402

_REAL = ("NEON_DB", "DATABASE_URL", "OPENAI_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
for _name in _REAL:
    os.environ.pop(_name, None)


@pytest.fixture(autouse=True)
def _no_real_credentials(monkeypatch):
    """Tests never spend the developer's LLM key or touch their GitHub account."""
    for name in ("OPENAI_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _database(monkeypatch):
    url = os.environ.get("CHOWKIDAAR_TEST_PG")
    if not url:
        monkeypatch.delenv("NEON_DB", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        yield
        return
    from app import db
    monkeypatch.setenv("NEON_DB", url)
    db.reset_connection()
    db.connect().execute("TRUNCATE " + ", ".join(db._TABLES))
    db.reset_connection()  # the mirror was loaded before the truncate
    yield
    db.reset_connection()


@pytest.fixture
def customer_app(tmp_path):
    """The demo customer app as a git repository in a temp dir (the product tree only holds its plain sources)."""
    from app import demo
    return demo.materialize(tmp_path / "customer-app")
