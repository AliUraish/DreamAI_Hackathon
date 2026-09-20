"""What happens after the pull request is open.

The repository's agent keeps looking at it: a reviewer's comment becomes another
round of work on the same branch (patch, checks, push, reply), and a merge is
confirmed by running the project's checks on the merged base branch before the
integration is marked healthy again.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import agents, db, events, gitops, notify, ui_events
from .config import settings
from .providers import get_provider
from .repair import RepairUnavailable, generate_repair
from .validate import run_validation

MAX_REVIEW_ROUNDS = 5


def open_pull_requests() -> list[dict[str, Any]]:
    return [m for m in db.select("migrations") if m.get("pr_number") and m.get("pr_state") == "open"]


def _review_target(graph: Any, branch: str) -> dict[str, Any]:
    """The node and option a `chowkidaar/relieve-<label>[-<option>]` branch was for, so the same review is not opened twice."""
    import re
    slug = branch.removeprefix("chowkidaar/relieve-")
    for node, data in graph.nodes(data=True):
        label = re.sub(r"[^a-z0-9]+", "-", str(data.get("label", "")).lower()).strip("-")
        if label and (slug == label or slug in {f"{label}-{option}" for option in ("split", "route", "limit")}):
            return {"node": node, "label": data.get("label"), "prefer": slug[len(label) + 1:] or None}
    return {}


def adopt(repo: dict[str, Any]) -> list[str]:
    """Pick up open pull requests Chowkidaar opened on this repository that it has no record of (a new database, a reconnected
    project). Everything recorded comes from GitHub: the title, the description, the diff. They are then watched like any other."""
    from . import ui_events
    from .graph import affected_from_usage
    if not repo.get("full_name") or not gitops.github_token():
        return []
    known = {m["pr_number"] for m in db.select("migrations", {"repo_id": repo["id"]}) if m.get("pr_number")}
    found = [pr for pr in gitops.open_pull_requests_by_prefix(repo["full_name"], "chowkidaar/") if pr["number"] not in known]
    if not found:
        return []
    from . import service
    usages, graph = service.analysis(repo)
    integrations = db.select("integrations", {"repo_id": repo["id"]})
    file_node = {graph.nodes[n].get("source_file"): n for u in usages.values() for n in affected_from_usage(graph, u)
                 if graph.nodes[n].get("source_location") == "L1" and not str(graph.nodes[n].get("label", "")).endswith("()")}
    adopted = []
    for pr in sorted(found, key=lambda p: p["number"]):
        patches = ui_events.build_patches(pr["diff"], file_node)
        paths = [p["path"] for p in patches]
        usage = next((u for u in usages.values() if set(paths) & set(u.files)), None)
        integration = next((i for i in integrations if usage and i["provider"] == usage.provider), None) or (integrations[0] if integrations else None)
        if not integration:
            continue
        kind = "performance" if pr["branch"].startswith("chowkidaar/relieve-") else "migration"
        migration = db.insert("migrations", {
            "id": db.new_id("mig"), "repo_id": repo["id"], "integration_id": integration["id"], "title": pr["title"], "status": "pr_opened", "kind": kind,
            "trigger": "adopted", "from_version": None, "to_version": None, "changes": [],
            "affected_files": [{"path": path, "depth": 0, "role": "changed by the pull request", "read_only": False} for path in paths], "steps": [], "validation": {},
            "patched_files": paths, "meta": {"adopted": True, "provider": integration["provider"], **(_review_target(graph, pr["branch"]) if kind == "performance" else {})}, "summary": pr["body"], "diff": pr["diff"],
            "branch": pr["branch"], "pr_url": pr["url"], "pr_number": pr["number"], "pr_state": "open",
            # Comments made before now were already seen by the agent that opened it; only new ones start a round.
            "review": {"seen": [c["id"] for c in gitops.pull_request_comments(repo["full_name"], pr["number"])], "rounds": []},
            "created_at": pr["created_at"].replace("Z", "+00:00"), "updated_at": db.now()})
        ui = lambda t, msg=None, **payload: ui_events.emit(migration["id"], {"t": t, "msg": msg, "kind": "pressure" if kind == "performance" else None, **payload}, repo_id=repo["id"])  # noqa: E731
        ui("stage", f"Found #{pr['number']} on GitHub, opened earlier by this project's agent", stage="patch")
        for patch in patches:
            ui("patch.done", f"{patch['path']} · +{patch['additions']} −{patch['deletions']}", patch=patch)
        ui("stage", stage="pr")
        ui("pr", f"GitHub pull request · #{pr['number']}", pr={"number": pr["number"], "title": pr["title"], "branch": pr["branch"], "base": pr["base"], "url": pr["url"],
           "files": len(patches), "additions": sum(p["additions"] for p in patches), "deletions": sum(p["deletions"] for p in patches)})
        ui("done", "Waiting for your review")
        events.emit("pr.adopted", f"Tracking #{pr['number']} again: {pr['title']}", repo_id=repo["id"], migration_id=migration["id"])
        agents.remember(repo["id"], "outcome", f"{pr['title']}: PR {pr['url']} (open, found on GitHub)")
        adopted.append(migration["id"])
    return adopted


def watch_once() -> None:
    """One pass over every open pull request. New comments and merges become agent tasks."""
    for migration in open_pull_requests():
        repo = db.get("repos", migration["repo_id"])
        if not repo or not repo.get("full_name"):
            continue
        try:
            state = gitops.pull_request(repo["full_name"], migration["pr_number"])
        except Exception as exc:  # network, revoked token: try again next pass
            print(f"pr watch: {repo['full_name']}#{migration['pr_number']}: {exc}")
            continue
        review = migration.get("review") or {"seen": [], "rounds": []}
        if state["state"] == "merged":
            db.update("migrations", migration["id"], {"pr_state": "merged", "merged_at": state["merged_at"], "status": "merged"})
            agents.submit(repo["id"], f"Confirming the merge of #{migration['pr_number']}", lambda m=migration, r=repo, s=state: confirm_merge(m, r, s),
                          migration_id=migration["id"])
        elif state["state"] == "closed":
            db.update("migrations", migration["id"], {"pr_state": "closed", "status": "closed"})
            db.update("integrations", migration["integration_id"], {"status": "breaking"})
            events.emit("pr.closed", f"Pull request #{migration['pr_number']} was closed without merging", repo_id=repo["id"], migration_id=migration["id"])
            notify.send(f"#{migration['pr_number']} was closed without merging", "The integration is still broken against the new contract.",
                        repo_id=repo["id"], level="warning", link=migration["pr_url"])
        else:
            fresh = [c for c in gitops.pull_request_comments(repo["full_name"], migration["pr_number"]) if c["id"] not in review["seen"]]
            if fresh and len(review["rounds"]) < MAX_REVIEW_ROUNDS:
                # Mark as seen before queueing, so the next pass does not queue the same comments again.
                db.update("migrations", migration["id"], {"review": {**review, "seen": [*review["seen"], *[c["id"] for c in fresh]]}})
                agents.submit(repo["id"], f"Addressing review on #{migration['pr_number']}",
                              lambda m=migration, r=repo, c=fresh: address_review(m["id"], r, c), migration_id=migration["id"])


def address_review(migration_id: str, repo: dict[str, Any], comments: list[dict[str, Any]]) -> None:
    try:
        _address_review(migration_id, repo, comments)
    finally:
        gitops.discard(settings.work_dir / f"{migration_id}-review")


def _address_review(migration_id: str, repo: dict[str, Any], comments: list[dict[str, Any]]) -> None:
    migration = db.get("migrations", migration_id)
    integration = db.get("integrations", migration["integration_id"])
    provider = get_provider(integration["provider"]) or {}
    ids = {"repo_id": repo["id"], "integration_id": integration["id"], "migration_id": migration_id}
    number, full_name = migration["pr_number"], repo["full_name"]
    ui = lambda t, msg=None, **payload: ui_events.emit(migration_id, {"t": t, "msg": msg, **payload}, repo_id=repo["id"])  # noqa: E731

    for c in comments:
        where = f" on {c['path']}" if c["path"] else ""
        events.emit("pr.comment", f"Review comment from {c['author']}{where}: {c['body'][:140]}", data=c, **ids)
        ui("pr.comment", f"Review from {c['author']}{where}", comment={"id": c["id"], "author": c["author"], "body": c["body"], "path": c["path"], "line": c["line"]})
        agents.remember(repo["id"], "review", f"Reviewer {c['author']} on PR #{number}{where}: {c['body'][:400]}")
    notify.send(f"{len(comments)} review comment(s) on #{number}", "The agent is working on them.", repo_id=repo["id"], link=migration["pr_url"])

    workdir = gitops.checkout_remote_branch(full_name, migration["branch"], settings.work_dir / f"{migration_id}-review", Path(repo["local_path"]))
    base_diff = gitops.git(workdir, "diff", f"origin/{migration['branch']}~{max(1, len((migration.get('review') or {}).get('rounds', [])) + 1)}", "HEAD", check=False) or migration.get("diff") or ""
    files = []
    for entry in migration["affected_files"]:
        path = workdir / entry["path"]
        if path.is_file():
            files.append({**entry, "content": path.read_text(encoding="utf-8", errors="ignore")})
    for c in comments:  # a reviewer may point at a file the first patch did not touch
        if c["path"] and (workdir / c["path"]).is_file() and not any(f["path"] == c["path"] for f in files):
            files.append({"path": c["path"], "depth": 0, "role": "reviewed", "read_only": False, "content": (workdir / c["path"]).read_text(errors="ignore")})

    agents.progress(repo["id"], "Rewriting the patch from the review")
    try:
        result = generate_repair(integration=integration, changes=migration["changes"], docs={"text": None, "error": "see the original pull request"},
                                 files=files, env_change=(migration.get("meta") or {}).get("env_change"), memory=agents.recall(repo["id"]),
                                 review={"comments": comments, "diff": base_diff})
    except RepairUnavailable as exc:
        gitops.comment_on_pull_request(full_name, number, f"I could not act on this review automatically: {exc}")
        events.emit("pr.review.failed", f"Could not address the review: {exc}", **ids)
        return
    if not result.files:
        reply = "No code change made. " + (" ".join(result.open_questions) or result.summary)
        gitops.comment_on_pull_request(full_name, number, reply, comments[0].get("reply_to"))
        events.emit("pr.review.answered", "Answered the review without changing code", **ids)
        return

    gitops.write_files(workdir, [f.model_dump() for f in result.files])
    agents.progress(repo["id"], "Running checks on the revised patch")
    validation = run_validation(workdir, repo["validation_commands"] or [], provider.get("validation_env", {}))
    checks = ", ".join(f"{c['name']}: {c['summary']}" for c in validation["checks"]) or "no checks configured"
    if validation["checks"] and not validation["passed"]:
        gitops.comment_on_pull_request(full_name, number, f"I tried to apply this review, but the project's checks fail with the change ({checks}), so I did not push it. "
                                       + (result.summary or ""), comments[0].get("reply_to"))
        events.emit("pr.review.failed", f"Review changes failed validation ({checks}); nothing pushed", **ids)
        notify.send(f"Review on #{number} needs you", f"The requested change fails the checks ({checks}).", repo_id=repo["id"], level="action", link=migration["pr_url"])
        return

    gitops.diff(workdir, [f.path for f in result.files])  # stages only what was rewritten
    sha = gitops.commit(workdir, f"Address review: {comments[0]['body'][:60]}")
    gitops.push_branch(workdir, full_name, migration["branch"])
    for c in comments:
        gitops.comment_on_pull_request(full_name, number, f"Done in {sha}. {result.summary}\n\nChecks: {checks}", c.get("reply_to"))
    review = (db.get("migrations", migration_id) or {}).get("review") or {"seen": [], "rounds": []}
    review["rounds"].append({"commit": sha, "comments": [c["id"] for c in comments], "summary": result.summary, "checks": checks, "at": db.now()})
    db.update("migrations", migration_id, {"review": review, "updated_at": db.now()})
    agents.remember(repo["id"], "outcome", f"Addressed review on PR #{number} in {sha}: {result.summary}")
    events.emit("pr.review.addressed", f"Review addressed in {sha} ({checks})", **ids)
    ui("pr.updated", f"Review addressed · {sha} · {checks}", update={"commit": sha, "summary": result.summary, "checks": checks, "files": [f.path for f in result.files]})
    notify.send(f"Review on #{number} addressed", f"{sha}: {result.summary}", repo_id=repo["id"], level="success", link=migration["pr_url"])


def confirm_merge(migration: dict[str, Any], repo: dict[str, Any], state: dict[str, Any]) -> None:
    try:
        _confirm_merge(migration, repo, state)
    finally:
        gitops.discard(settings.work_dir / f"{migration['id']}-merged")


def _confirm_merge(migration: dict[str, Any], repo: dict[str, Any], state: dict[str, Any]) -> None:
    """The user merged. Prove the base branch is healthy before saying so."""
    from . import service
    integration = db.get("integrations", migration["integration_id"])
    provider = get_provider(integration["provider"]) or {}
    ids = {"repo_id": repo["id"], "integration_id": integration["id"], "migration_id": migration["id"]}
    agents.progress(repo["id"], f"Running checks on {state['base']} after the merge")
    workdir = gitops.checkout_remote_branch(repo["full_name"], state["base"], settings.work_dir / f"{migration['id']}-merged", Path(repo["local_path"]))
    validation = run_validation(workdir, repo["validation_commands"] or [], provider.get("validation_env", {}))
    checks = ", ".join(f"{c['name']}: {c['summary']}" for c in validation["checks"]) or "no checks configured"
    healthy = validation["passed"] or not validation["checks"]

    version = migration.get("to_version") if migration["trigger"] != "env-change" else integration.get("version")
    db.update("integrations", integration["id"], {"status": "healthy" if healthy else "needs_review", "version": version or integration.get("version")})
    db.update("migrations", migration["id"], {"status": "merged" if healthy else "needs_review", "validation": {**(migration.get("validation") or {}), "merged": validation}})
    if Path(repo["local_path"]).resolve().is_relative_to(settings.repos_dir.resolve()):
        gitops.git(Path(repo["local_path"]), "pull", "--ff-only", check=False)  # our own clone; a user's checkout is never touched
    service.sync_integrations(repo)  # the merged code is the new map

    message = f"#{migration['pr_number']} merged and verified on {state['base']} ({checks})" if healthy else f"#{migration['pr_number']} merged, but checks fail on {state['base']} ({checks})"
    agents.remember(repo["id"], "outcome", message + f". {migration['title']}.")
    events.emit("pr.merged", message, **ids)
    ui_events.emit(migration["id"], {"t": "pr.merged", "msg": message, "merged": {"verified": healthy, "checks": checks, "base": state["base"], "at": state["merged_at"]}}, repo_id=repo["id"])
    notify.send(message, migration["title"], repo_id=repo["id"], level="success" if healthy else "warning", link=migration["pr_url"])
