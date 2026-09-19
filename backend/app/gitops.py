"""Git working copies and GitHub pull requests."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx

from .config import settings

GIT_IDENTITY = ["-c", "user.name=Chowkidaar", "-c", "user.email=bot@chowkidaar.invalid"]


def git(cwd: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()[-400:]}")
    return result.stdout.strip()


_gh_token: tuple[float, str | None] | None = None


def github_token() -> str | None:
    global _gh_token
    if settings.github_token:
        return settings.github_token
    import time
    if _gh_token and time.monotonic() - _gh_token[0] < 300:
        return _gh_token[1]
    token = _gh_cli_token()
    _gh_token = (time.monotonic(), token)
    return token


def _gh_cli_token() -> str | None:
    try:
        result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
        return result.stdout.strip() or None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def parse_full_name(remote_url: str | None) -> str | None:
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", remote_url or "")
    return match.group(1) if match else None


def describe_repo(path: Path) -> dict[str, Any]:
    remote = git(path, "remote", "get-url", "origin", check=False) or None
    branch = git(path, "rev-parse", "--abbrev-ref", "HEAD", check=False) or "main"
    return {"remote_url": remote, "full_name": parse_full_name(remote), "default_branch": branch}


def clone_from_github(full_name: str) -> Path:
    target = settings.repos_dir / full_name.replace("/", "__")
    if target.exists():
        git(target, "pull", "--ff-only", check=False)
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    token = github_token()
    url = f"https://x-access-token:{token}@github.com/{full_name}.git" if token else f"https://github.com/{full_name}.git"
    result = subprocess.run(["git", "clone", "--depth", "50", url, str(target)], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"could not clone {full_name}: {result.stderr.replace(token or '\0', '***')[-300:]}")
    git(target, "remote", "set-url", "origin", f"https://github.com/{full_name}.git")
    return target


def discard(*workdirs: Path) -> None:
    """Remove a run's working copy. The change lives on the pushed branch (or in the stored diff), not here."""
    for workdir in workdirs:
        shutil.rmtree(workdir, ignore_errors=True)


def link_dependencies(source: Path, target: Path, max_depth: int = 6) -> None:
    """Reuse installed dependencies so checks start in seconds. Workspaces keep their own `node_modules` next to each
    package (and packages nest), so every one of them is linked, not only the one at the root."""
    import os
    for current, dirs, _ in os.walk(source):
        relative = Path(current).relative_to(source)
        if len(relative.parts) >= max_depth:
            dirs[:] = []
            continue
        for name in [d for d in dirs if d in {"node_modules", ".venv"}]:
            link = target / relative / name
            if link.parent.is_dir() and not link.exists():
                link.symlink_to(Path(current) / name)
        dirs[:] = [d for d in dirs if d not in {"node_modules", ".venv", ".git", "dist", ".next", ".wrangler"}]


def make_working_copy(source: Path, migration_id: str, base_branch: str, branch: str) -> Path:
    """A throwaway clone so a migration never touches the connected checkout."""
    workdir = settings.work_dir / migration_id
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "--quiet", "--branch", base_branch, str(source), str(workdir)], check=True, capture_output=True)
    git(workdir, "checkout", "-b", branch)
    # The dependency symlinks below must never be committed, whatever the repo's .gitignore says.
    with (workdir / ".git" / "info" / "exclude").open("a") as exclude:
        exclude.write("\nnode_modules\n.venv\n*.tsbuildinfo\n")
    link_dependencies(source, workdir)
    return workdir


def write_files(workdir: Path, files: list[dict[str, str]]) -> list[str]:
    written = []
    for f in files:
        target = (workdir / f["path"]).resolve()
        if not target.is_relative_to(workdir.resolve()):
            raise RuntimeError(f"refusing to write outside the repository: {f['path']}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f["content"])
        written.append(f["path"])
    return written


def diff(workdir: Path, paths: list[str] | None = None) -> str:
    """Stage and return the change. Only the files the agent wrote are staged: a project's checks leave artifacts behind
    (tsconfig.tsbuildinfo, coverage, caches) that its .gitignore may not cover, and they do not belong in a pull request."""
    if paths:
        git(workdir, "add", "--", *paths)
    else:
        git(workdir, "add", "-A")
    return git(workdir, "diff", "--cached")


def commit(workdir: Path, message: str) -> str:
    git(workdir, *GIT_IDENTITY, "commit", "--quiet", "-m", message)
    return git(workdir, "rev-parse", "--short", "HEAD")


def open_pull_request(workdir: Path, *, full_name: str, branch: str, base: str, title: str, body: str) -> dict[str, Any]:
    token = github_token()
    if not token:
        raise RuntimeError("no GitHub token (set CHOWKIDAAR_GITHUB_TOKEN or run `gh auth login`)")
    push_url = f"https://x-access-token:{token}@github.com/{full_name}.git"
    result = subprocess.run(["git", "push", "--force", push_url, f"{branch}:{branch}"], cwd=workdir, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("git push failed: " + result.stderr.replace(token, "***")[-300:])
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    api = f"https://api.github.com/repos/{full_name}/pulls"
    response = httpx.post(api, headers=headers, json={"title": title, "head": branch, "base": base, "body": body}, timeout=30)
    if response.status_code == 422:  # a PR for this branch already exists: update it
        owner = full_name.split("/")[0]
        existing = httpx.get(api, headers=headers, params={"head": f"{owner}:{branch}", "state": "open"}, timeout=30).json()
        if existing:
            number = existing[0]["number"]
            response = httpx.patch(f"{api}/{number}", headers=headers, json={"title": title, "body": body}, timeout=30)
    response.raise_for_status()
    pr = response.json()
    return {"url": pr["html_url"], "number": pr["number"]}


# --- pull request lifecycle ----------------------------------------------------------

BOT_MARKER = "<!-- chowkidaar -->"  # the bot posts with the user's token, so its own comments are told apart by this


def _github(method: str, path: str, **kwargs: Any) -> Any:
    token = github_token()
    if not token:
        raise RuntimeError("no GitHub token (set CHOWKIDAAR_GITHUB_TOKEN or run `gh auth login`)")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    response = httpx.request(method, f"https://api.github.com{path}", headers=headers, timeout=30, **kwargs)
    response.raise_for_status()
    return response.json() if response.content else None


def pull_request(full_name: str, number: int) -> dict[str, Any]:
    pr = _github("GET", f"/repos/{full_name}/pulls/{number}")
    return {"state": "merged" if pr.get("merged") else pr["state"], "merged_at": pr.get("merged_at"), "head_sha": pr["head"]["sha"],
            "merge_commit": pr.get("merge_commit_sha"), "base": pr["base"]["ref"], "url": pr["html_url"]}


def _is_bot(user: dict[str, Any] | None) -> bool:
    """Deploy previews, CI summaries and coverage reports are posted by apps, not by reviewers."""
    user = user or {}
    return user.get("type") == "Bot" or str(user.get("login", "")).endswith("[bot]")


def pull_request_comments(full_name: str, number: int) -> list[dict[str, Any]]:
    """What people said on the pull request: conversation comments, inline review comments and review summaries, oldest
    first. Comments from apps (Cloudflare, Vercel, CI bots) and Chowkidaar's own replies are not review feedback."""
    found = []
    for c in _github("GET", f"/repos/{full_name}/issues/{number}/comments", params={"per_page": 100}):
        if _is_bot(c.get("user")):
            continue
        found.append({"id": f"issue:{c['id']}", "author": c["user"]["login"], "body": c["body"] or "", "path": None, "line": None, "at": c["created_at"], "reply_to": None})
    for c in _github("GET", f"/repos/{full_name}/pulls/{number}/comments", params={"per_page": 100}):
        if _is_bot(c.get("user")):
            continue
        found.append({"id": f"review:{c['id']}", "author": c["user"]["login"], "body": c["body"] or "", "path": c.get("path"),
                      "line": c.get("line") or c.get("original_line"), "at": c["created_at"], "reply_to": c["id"]})
    for r in _github("GET", f"/repos/{full_name}/pulls/{number}/reviews", params={"per_page": 100}):
        if (r.get("body") or "").strip() and not _is_bot(r.get("user")):
            found.append({"id": f"summary:{r['id']}", "author": r["user"]["login"], "body": r["body"], "path": None, "line": None, "at": r.get("submitted_at") or "", "reply_to": None})
    return sorted((c for c in found if BOT_MARKER not in c["body"]), key=lambda c: c["at"])


def comment_on_pull_request(full_name: str, number: int, body: str, reply_to: int | None = None) -> None:
    body = f"{body}\n\n{BOT_MARKER}"
    if reply_to:
        _github("POST", f"/repos/{full_name}/pulls/{number}/comments/{reply_to}/replies", json={"body": body})
    else:
        _github("POST", f"/repos/{full_name}/issues/{number}/comments", json={"body": body})


def push_branch(workdir: Path, full_name: str, branch: str) -> None:
    token = github_token()
    result = subprocess.run(["git", "push", f"https://x-access-token:{token}@github.com/{full_name}.git", f"{branch}:{branch}"],
                            cwd=workdir, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("git push failed: " + result.stderr.replace(token or "\0", "***")[-300:])


def checkout_remote_branch(full_name: str, branch: str, target: Path, deps_from: Path | None = None) -> Path:
    """A fresh clone of one branch from GitHub (the PR branch for a review round, the base branch after a merge)."""
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    token = github_token()
    url = f"https://x-access-token:{token}@github.com/{full_name}.git" if token else f"https://github.com/{full_name}.git"
    result = subprocess.run(["git", "clone", "--quiet", "--depth", "30", "--branch", branch, url, str(target)], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"could not fetch {branch}: " + result.stderr.replace(token or "\0", "***")[-300:])
    git(target, "remote", "set-url", "origin", f"https://github.com/{full_name}.git")
    with (target / ".git" / "info" / "exclude").open("a") as exclude:
        exclude.write("\nnode_modules\n.venv\n*.tsbuildinfo\n")
    if deps_from:
        link_dependencies(deps_from, target)
    return target
