from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent

# Provider and LLM keys (ANTHROPIC_API_KEY, ELEVENLABS_API_KEY, ...) are read from
# the process environment by their SDKs, so backend/.env is loaded into it.
load_dotenv(BACKEND_ROOT / ".env")

# The product used to be called PatchLayer. Settings written under the old prefix keep working.
for _name, _value in list(os.environ.items()):
    if _name.startswith("PATCH" + "LAYER_"):
        os.environ.setdefault("CHOWKIDAAR_" + _name.split("_", 1)[1], _value)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CHOWKIDAAR_", env_file=BACKEND_ROOT / ".env", extra="ignore")

    # Everything the agents produce (working copies, graphs, audit logs, keys) lives outside the product's source tree,
    # so the product folder never contains another project's repository. Override with CHOWKIDAAR_DATA_DIR.
    data_dir: Path = Path.home() / ".chowkidaar"
    cors_origins: list[str] = ["*"]

    # Demo provider (the Acme Orders mock).
    acme_orders_url: str = "http://localhost:4010"

    # LLM. OPENAI_KEY (backend/.env) selects OpenAI; without it, Anthropic credentials are used if present.
    openai_model: str = "gpt-5"
    llm_model: str = "claude-opus-5"  # Anthropic fallback
    llm_effort: str = "high"
    max_repair_attempts: int = 2

    # GitHub. Falls back to `gh auth token` when unset.
    github_token: str | None = None
    open_prs: bool = True

    # How far from an API call site a file can be and still be "affected".
    graph_depth: int = 3
    poll_interval_seconds: int = 0  # 0 = no background polling; checks run on demand
    validation_timeout_seconds: int = 300
    env_watch_seconds: int = 5  # how often connected repos' env files are re-read; 0 = only on demand
    prefetch_explanations: bool = True  # have the model explain every connection in the background once a project is mapped
    audit_seconds: int = 300  # how often each project's agent audits traffic and logs; silent unless it finds pressure. 0 = off
    pr_watch_seconds: int = 20  # how often open pull requests are checked for review comments and merges; 0 = off
    app_url: str = "http://localhost:5173"  # where the UI is served
    public_url: str = "http://localhost:8000"  # where a connected project reaches this backend

    @property
    def db_path(self) -> Path:
        return self.data_dir / "chowkidaar.db"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def repos_dir(self) -> Path:
        return self.data_dir / "repos"


settings = Settings()
