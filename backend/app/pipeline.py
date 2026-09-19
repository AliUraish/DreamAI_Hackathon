"""The migration pipeline: docs -> affected files -> reproduce -> patch -> validate -> pull request."""

from __future__ import annotations

import re
import traceback
from pathlib import Path
from typing import Any

from . import agents, db, envwatch, events, gitops, notify, poller, ui_events
from .config import settings
from .docs import fetch_docs
from .graph import _find_nodes, affected_files, affected_from_usage, trace_hits
from .providers import get_provider
from .repair import RepairUnavailable, generate_repair
from .scanner import TEST_MARKERS
from .schema import summarize_change
from .service import analysis
from .validate import run_validation

STEPS = [
    ("detect", "API change detected"),
    ("docs", "Migration guide retrieved"),
    ("affected", "Affected files identified"),
    ("reproduce", "Breakage reproduced"),
    ("patch", "Code migration generated"),
    ("validate", "Validation passed"),
    ("pr", "Pull request opened"),
]


class _Run:
    def __init__(self, migration: dict[str, Any]):
        self.migration = migration
        self.id = migration["id"]
        self.steps = [{"key": k, "label": label, "status": "pending", "detail": None} for k, label in STEPS]
        self.ids = {"repo_id": migration["repo_id"], "integration_id": migration["integration_id"], "migration_id": self.id}

    def step(self, key: str, status: str, detail: str | None = None, *, announce: bool = True, data: dict | None = None) -> None:
        step = next(s for s in self.steps if s["key"] == key)
        step.update(status=status, detail=detail)
        db.update("migrations", self.id, {"steps": self.steps, "updated_at": db.now()})
        if status in {"done", "running"}:
            agents.progress(self.migration["repo_id"], detail or step["label"])
        if announce and status in {"done", "failed", "skipped"}:
            events.emit(f"migration.{key}.{status}", detail or step["label"], data=data, **self.ids)

    def ui(self, t: str, msg: str | None = None, **payload: Any) -> None:
        ui_events.emit(self.id, {"t": t, "msg": msg, **payload}, repo_id=self.migration["repo_id"])

    def save(self, **values: Any) -> None:
        db.update("migrations", self.id, {**values, "updated_at": db.now()})


def _is_test(path: str) -> bool:
    return any(marker in f"/{path}" for marker in TEST_MARKERS)


def _pr_body(run: _Run, *, integration, changes, files, docs, before, after, result, kind) -> str:
    lines = [f"## {run.migration['title']}", ""]
    if kind == "investigation":
        lines += ["> Chowkidaar detected a breaking API change but could **not** produce a migration that passes this "
                  "repository's checks. No application code is changed in this pull request; it carries the evidence.", ""]
    if result and result.summary:
        lines += [result.summary, ""]
    lines += ["### Breaking changes", *[f"- `{c['kind']}` {summarize_change(c)}" for c in changes if c["severity"] == "BREAKING"], ""]
    lines += ["### Affected files", *[f"- `{f['path']}` ({'API call site' if f['depth'] == 0 else f'{f['depth']} hop(s) from the API call'})" for f in files], ""]
    if result and result.confirmed_changes:
        lines += ["### What was changed, and the evidence", *[f"- {c}" for c in result.confirmed_changes], ""]
    lines += ["### Validation", "| Check | Before | After |", "|---|---|---|"]
    before_checks = {c["name"]: c for c in (before or {}).get("checks", [])}
    for check in (after or before or {}).get("checks", []):
        b = before_checks.get(check["name"])
        after_cell = f"{'✅' if check['passed'] else '❌'} {check['summary']}" if after else "not run"
        lines.append(f"| {check['name']} | {('✅' if b['passed'] else '❌') + ' ' + b['summary'] if b else 'n/a'} | {after_cell} |")
    lines.append("")
    questions = list(result.open_questions) if result else []
    touched_tests = [p for p in (run.migration.get("patched_files") or []) if _is_test(p)]
    if touched_tests:
        questions.insert(0, "Test files were edited to update doubles of the old provider's wire format: " + ", ".join(f"`{p}`" for p in touched_tests))
    if questions:
        lines += ["### Please double check", *[f"- {q}" for q in questions], ""]
    if docs.get("url"):
        lines += [f"Migration guide: {docs['url']}", ""]
    lines += ["---", f"Opened automatically by Chowkidaar for **{integration['name']}** (trigger: {run.migration['trigger']}). Review before merging; Chowkidaar never merges."]
    return "\n".join(lines)


def run_migration(migration_id: str) -> None:
    migration = db.get("migrations", migration_id)
    if migration is None:
        return
    run = _Run(migration)
    try:
        _run(run)
    except Exception as exc:  # keep the failure visible in the UI instead of dying in a worker thread
        traceback.print_exc()
        run.save(status="failed", error=str(exc))
        db.update("integrations", migration["integration_id"], {"status": "breaking"})
        events.emit("migration.failed", f"Migration failed: {exc}", **run.ids)
        run.ui("done", f"Migration failed: {exc}")
    finally:
        gitops.discard(settings.work_dir / migration_id)


def _run(run: _Run) -> None:
    migration = run.migration
    integration = db.get("integrations", migration["integration_id"])
    repo = db.get("repos", migration["repo_id"])
    provider = get_provider(integration["provider"]) or {}
    meta = migration["meta"] or {}
    changes = migration["changes"]
    run.save(status="running", error=None)
    db.update("integrations", integration["id"], {"status": "migrating"})
    run.step("detect", "done", f"{len([c for c in changes if c['severity'] == 'BREAKING'])} breaking changes", announce=False)
    provider_node = f"provider:{integration['provider']}"
    versions = f"{migration.get('from_version') or 'current'} → {migration.get('to_version') or 'changed'}"
    gone = next((c for c in changes if c["kind"] == "endpoint-gone"), None)
    env_change = meta.get("env_change")
    source = (f"environment change confirmed by the user · {env_change['details']['from_env']} → {env_change['details']['to_env']}" if env_change
              else "release announced by the provider" if migration["trigger"] == "provider-release"
              else f"observed on the live API · {gone['path']} returned {gone['after']}" if gone else "observed on the live API · response shape changed")
    run.ui("stage", stage="detect")
    run.ui("release", (f"Provider change confirmed · {versions}" if env_change else f"API release detected · {integration['name']} {versions}"), providerId=provider_node,
           **{"from": migration.get("from_version") or "current", "to": migration.get("to_version") or "changed"}, source=source)

    # 1. documentation
    docs = fetch_docs(meta.get("docs_url") or integration.get("docs_url"))
    if docs.get("text"):
        note = " (truncated to fit)" if docs.get("truncated") else ""
        run.step("docs", "done", f"Migration guide retrieved{note}", data={"url": docs["url"]})
    else:
        run.step("docs", "skipped", f"No migration guide found ({docs.get('error')})")

    # 2. affected files: scanner seeds + Graphify blast radius
    usages, graph = analysis(repo, refresh=True)
    root = Path(repo["local_path"])
    # An env-driven migration starts from the old provider's call sites plus every reader of the old variable.
    usage = envwatch.usage_for_change(root, usages, env_change) if env_change else usages.get(integration["provider"])
    if usage is None or not usage.files:
        raise RuntimeError("the repository no longer references this API")
    files = affected_files(graph, usage)
    # Tests are read-only context, except in a provider switch, where their test doubles encode the old wire format.
    tests_editable = bool(env_change and env_change["kind"] == "provider-switched")
    for f in files:
        f["read_only"] = _is_test(f["path"]) and not tests_editable
        f["content"] = (root / f["path"]).read_text(encoding="utf-8", errors="ignore")
    editable = [f for f in files if not f["read_only"]]
    listed = [{k: f[k] for k in ("path", "depth", "role", "read_only")} for f in files]
    run.save(affected_files=listed)
    db.update("integrations", integration["id"], {"files": [{k: f[k] for k in ("path", "depth", "role")} for f in files]})
    run.step("affected", "done", f"{len(editable)} affected files identified", data={"files": [f["path"] for f in editable]})

    included = set(affected_from_usage(graph, usage))
    file_nodes, symbol_nodes = _find_nodes(graph, usage)
    file_node = {graph.nodes[n].get("source_file"): n for n in included if graph.nodes[n].get("source_location") == "L1" and not str(graph.nodes[n].get("label", "")).endswith("()")}
    ui_changes = ui_events.build_changes(changes, docs.get("text"), graph, files, included, sorted([*symbol_nodes, *file_nodes.values()]))
    run.ui("stage", "Comparing the old and new contracts", stage="diff")
    for change in ui_changes:
        run.ui("change", f"Breaking · {change['before']} → {change['after']}", change=change)
    run.ui("stage", f"Walking the code graph from {integration['name']}", stage="trace")
    hits = trace_hits(graph, usage)
    by_role = {role: len({graph.nodes[h["nodeId"]].get("source_file") for h in hits if h["role"] == role}) for role in ("change", "dependent", "test")}
    trace_msg = f"{by_role['change']} call-site file(s) · {by_role['dependent']} dependent file(s) · {by_role['test']} test file(s)"
    for index, hit in enumerate(hits):
        run.ui("hit", trace_msg if index == len(hits) - 1 else None, hit=hit)
    run.ui("stage", stage="docs")
    if docs.get("text"):
        title = next((line.lstrip("# ").strip() for line in docs["text"].splitlines() if line.startswith("#")), "Migration guide")
        run.ui("docs", "Migration guide retrieved", title=title, url=docs["url"])
        for excerpt in ui_events.build_excerpts(ui_changes, docs["text"]):
            run.ui("excerpt", excerpt=excerpt)

    # 3. working copy + reproduce the breakage on the unpatched code
    target = (env_change.get("to_provider") or env_change["details"]["to_env"]) if env_change else (migration.get("to_version") or meta.get("fingerprint", "drift"))
    slug = re.sub(r"[^a-z0-9._-]+", "-", f"{integration['provider']}-{'to-' if env_change else ''}{target}".lower()).strip("-")
    branch = f"chowkidaar/{slug}"
    workdir = gitops.make_working_copy(root, run.id, repo["default_branch"], branch)
    commands = repo["validation_commands"] or []
    env = provider.get("validation_env", {})
    before = run_validation(workdir, commands, env) if commands else {"passed": False, "checks": []}
    if not commands:
        run.step("reproduce", "skipped", "No test or build command found in this repository")
    elif before["passed"]:
        run.step("reproduce", "done", "Existing checks still pass: they stub the network, so they cannot see the provider change" if env_change
                 else "Current code still passes; the change is not yet enforced")
    else:
        failing = ", ".join(c["summary"] for c in before["checks"] if not c["passed"])
        run.step("reproduce", "done", f"Confirmed on current code: {failing}")
    run.save(validation={"before": before})

    # 4 + 5. generate, validate, retry once with the failure output
    result, after, previous, kind, reason = None, None, None, "repair", None
    started: set[str] = set()
    run.ui("stage", f"Generating migration for {len(editable)} files", stage="patch")
    for path in usage.files:  # the call sites are certain to change; the rest is the model's call
        if path in file_node:
            started.add(path)
            run.ui("patch.start", nodeId=file_node[path])
    for attempt in range(1, settings.max_repair_attempts + 1):
        try:
            agents.progress(repo["id"], f"Writing the migration (attempt {attempt})")
            result = generate_repair(integration=integration, changes=changes, docs=docs, files=files, previous_attempt=previous,
                                     env_change=env_change, memory=agents.recall(repo["id"]))
        except RepairUnavailable as exc:
            kind, reason = "investigation", str(exc)
            run.step("patch", "failed", f"Could not generate a migration: {exc}")
            break
        if result.outcome == "needs_investigation" or not result.files:
            kind, reason = "investigation", "the model judged the evidence insufficient for a safe change"
            run.step("patch", "skipped", "Evidence insufficient for an automatic migration")
            break
        patched = gitops.write_files(workdir, [f.model_dump() for f in result.files])
        run.migration["patched_files"] = patched
        run.step("patch", "done", f"Code migration generated ({len(patched)} files, attempt {attempt})", data={"files": patched})
        run.save(patched_files=patched)
        for patch in ui_events.build_patches(gitops.diff(workdir, patched), file_node):
            if patch["path"] not in started:
                run.ui("patch.start", nodeId=patch["nodeId"])
            started.add(patch["path"])
            run.ui("patch.done", f"Patched {patch['path']} · +{patch['additions']} −{patch['deletions']}", patch=patch)
        if attempt == 1:
            run.ui("stage", "Running checks against the new API", stage="verify")
            baseline_checks = ui_events.build_checks(before, "baseline", file_node)
            for index, check in enumerate(baseline_checks):
                failed_n = len([c for c in baseline_checks if c["status"] == "failed"])
                run.ui("check", f"Baseline without the patch · {failed_n} of {len(baseline_checks)} checks fail" if index == len(baseline_checks) - 1 else None, check=check)
            for check in ui_events.build_checks(before, "patched", file_node, status_override="running"):
                run.ui("check", check=check)
        after = run_validation(workdir, commands, env) if commands else {"passed": True, "checks": []}
        run.save(validation={"before": before, "after": after})
        for check in ui_events.build_checks(after, "patched", file_node):
            run.ui("check", check=check)
        if after["passed"]:
            run.step("validate", "done", ", ".join(f"{c['name']}: {c['summary']}" for c in after["checks"]) or "No checks configured")
            break
        failed_output = "\n\n".join(f"$ {c['cmd']}\n{c['output']}" for c in after["checks"] if not c["passed"])
        previous = {"files": [f.model_dump() for f in result.files], "output": failed_output}
        if attempt == settings.max_repair_attempts:
            kind, reason = "investigation", "the generated migration did not pass validation"
            run.step("validate", "failed", "Validation failed after retry: " + ", ".join(c["summary"] for c in after["checks"] if not c["passed"]))
        else:
            run.step("validate", "failed", "Validation failed; retrying with the failure output", announce=True)

    # An investigation PR changes no application code: reset and attach a report instead.
    if kind == "investigation":
        for path in started:  # close any "patching" spinner in the UI: nothing was changed
            run.ui("patch.done", patch={"nodeId": file_node[path], "path": path, "additions": 0, "deletions": 0, "hunks": []})
        gitops.git(workdir, "reset", "--hard", "--quiet", "HEAD")  # index too: the mid-run diff staged the failed patch
        gitops.git(workdir, "clean", "-fdq")
        run.save(patched_files=[])

    body = _pr_body(run, integration=integration, changes=changes, files=editable, docs=docs, before=before,
                    after=after if kind == "repair" else None, result=result, kind=kind)
    title = migration["title"] if kind == "repair" else f"Investigate: {migration['title']}"
    if kind == "investigation":
        report = workdir / "chowkidaar" / f"{slug}.md"
        report.parent.mkdir(exist_ok=True)
        extra = f"\n\n### Why no automatic migration\n{reason}\n"
        if after and not after["passed"]:
            extra += "\n### Last validation output\n```\n" + "\n".join(c["output"][-2000:] for c in after["checks"] if not c["passed"]) + "\n```\n"
        report.write_text(body + extra)

    diff = gitops.diff(workdir, [str(report.relative_to(workdir))] if kind == "investigation" else [f.path for f in result.files])
    gitops.commit(workdir, title)
    run.save(kind=kind, diff=diff, branch=branch, title=title, summary=(result.summary if result else reason), error=reason)

    # 6. pull request
    pr_url = None
    if repo.get("full_name") and settings.open_prs:
        try:
            pr = gitops.open_pull_request(workdir, full_name=repo["full_name"], branch=branch, base=repo["default_branch"], title=title, body=body)
            pr_url = pr["url"]
            run.save(pr_number=pr["number"], pr_state="open", review={"seen": [], "rounds": []})
            run.step("pr", "done", f"GitHub pull request opened: #{pr['number']}", data={"url": pr_url})
            patches = ui_events.build_patches(diff, file_node)
            run.ui("stage", stage="pr")
            run.ui("pr", f"GitHub pull request opened · #{pr['number']}", pr={
                "number": pr["number"], "title": title, "branch": branch, "base": repo["default_branch"], "url": pr_url, "files": len(patches),
                "additions": sum(p["additions"] for p in patches), "deletions": sum(p["deletions"] for p in patches)})
        except Exception as exc:
            run.step("pr", "failed", f"Could not open the pull request: {exc}")
    else:
        why = "CHOWKIDAAR_OPEN_PRS is off" if repo.get("full_name") else "the repository has no GitHub remote"
        run.step("pr", "skipped", f"Branch {branch} is ready locally ({why})")

    status = "needs_review" if kind == "investigation" else ("pr_opened" if pr_url else "ready_local")
    run.save(status=status, pr_url=pr_url)

    # Track the successor from now on, so the next poll compares against the new contract.
    successors = meta.get("successors") or {}
    endpoints, baseline = integration["endpoints"], dict(integration["baseline"] or {})
    for endpoint in endpoints:
        new_url = successors.get(endpoint["id"])
        if new_url:
            endpoint["url"] = new_url
            endpoint["path"] = "/" + new_url.split("://", 1)[-1].split("/", 1)[-1]
            sampled = poller.sample(endpoint["method"], new_url)
            if sampled["ok"]:
                baseline[endpoint["id"]] = poller.snapshot(sampled["body"])
    db.update("integrations", integration["id"], {
        "status": "needs_review" if kind == "investigation" else "migration_ready",
        "endpoints": endpoints, "baseline": baseline})
    outcome = (f"{title}: " + (f"opened PR {pr_url}" if pr_url else f"branch {branch} ready locally") + f". {result.summary if result else ''}"
               if kind == "repair" else f"{title}: no automatic migration ({reason})")
    agents.remember(repo["id"], "outcome", outcome)
    if kind == "repair":
        notify.send("Pull request opened" if pr_url else "Migration validated", migration["title"], repo_id=repo["id"], level="success", link=pr_url)
    else:
        notify.send("Needs your review", f"{migration['title']}: {reason}", repo_id=repo["id"], level="action", link=pr_url)
    closing = ("Migration ready for review" if pr_url else f"Branch {branch} ready locally") if kind == "repair" else f"No automatic migration: {reason}"
    run.ui("done", closing)
    events.emit("migration.completed", "Migration ready for review" if kind == "repair" else "Investigation ready for review", **run.ids,
                data={"pr_url": pr_url, "kind": kind})
