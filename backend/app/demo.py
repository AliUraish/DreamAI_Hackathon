"""The demo customer app as a git repository, outside the product's own source tree.

`demo/customer-app` in this repository is plain source files. Chowkidaar only works on git repositories, so the demo
gets its own copy, with its own history, under the data directory. The product folder never contains a nested repository.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .config import BACKEND_ROOT, settings

SOURCE = BACKEND_ROOT.parent / "demo" / "customer-app"
FAKE_ENV = "ORDERS_API_URL=http://localhost:4010\nANTHROPIC_API_KEY=demo-anthropic-value\n"


def materialize(target: Path | None = None) -> Path:
    """Create (or refresh the sources of) the demo repository and return its path."""
    target = target or settings.data_dir / "demo" / "customer-app"
    target.mkdir(parents=True, exist_ok=True)
    for item in SOURCE.iterdir():
        if item.name in {"node_modules", ".git", ".env", "graphify-out"}:
            continue
        destination = target / item.name
        if item.is_dir():
            shutil.copytree(item, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(item, destination)
    if (SOURCE / "node_modules").is_dir() and not (target / "node_modules").exists():
        (target / "node_modules").symlink_to(SOURCE / "node_modules")  # installed once, next to the sources
    if not (target / ".env").exists():
        (target / ".env").write_text(FAKE_ENV)
    git = lambda *args: subprocess.run(["git", "-C", str(target), *args], capture_output=True, text=True)  # noqa: E731
    if not (target / ".git").exists():
        git("init", "-q", "-b", "main")
    git("add", "-A")
    if git("status", "--porcelain").stdout.strip():
        git("-c", "user.name=Demo", "-c", "user.email=demo@example.com", "commit", "-q", "-m", "Acme app (demo)")
    return target
