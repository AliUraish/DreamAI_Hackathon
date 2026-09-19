"""LLM repair: turns (contract diff + provider docs + affected files) into a migration.

The model returns whole files, not diffs - a whole file either parses or it does
not, and we compute the diff ourselves with git.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from . import llm
from .schema import summarize_change


class PatchedFile(BaseModel):
    path: str = Field(description="Repository-relative path, exactly as given in the input.")
    content: str = Field(description="The complete new contents of the file.")
    reason: str = Field(description="One sentence: what changed in this file and why.")


class RepairResult(BaseModel):
    outcome: Literal["patched", "needs_investigation"] = Field(
        description="'patched' when you are confident in the migration; 'needs_investigation' when the evidence is "
                    "insufficient to change code safely (then return no files).")
    summary: str = Field(description="2-4 sentences for the pull request description.")
    files: list[PatchedFile]
    confirmed_changes: list[str] = Field(description="Each breaking change you acted on, and the evidence for it (docs or observed response).")
    open_questions: list[str] = Field(description="Anything a human reviewer should double check. Empty when none.")


class RepairUnavailable(RuntimeError):
    """The LLM could not be reached or declined; the pipeline falls back to an investigation PR."""


SYSTEM = """You maintain API integrations. An external API that this repository depends on has changed, and you are \
writing the migration that will be opened as a pull request for a developer to review.

You are given: the contract changes we observed on the live API, the provider's migration documentation when we could \
find it, and the full contents of the files that depend on this API (the call sites and everything downstream of them).

How to decide what to change:
- The observed diff tells you a field disappeared and another appeared. It cannot tell you whether the *meaning* changed. \
The documentation can: units, formats, enum values, required request fields, authentication. When the docs and the observed \
diff disagree, trust the observed diff about shape and the docs about meaning, and say so in open_questions.
- A "rename-candidate" is a guess from shape alone. Treat it as confirmed only if the docs or the field semantics support it.
- Keep the blast radius small. Prefer translating at the API boundary (the client module) so that the rest of the \
application keeps its existing types, names and units. Only edit a downstream file when the boundary cannot absorb the change.
- Preserve the application's observable behaviour. If the app displayed dollars before, it still displays dollars.
- Files marked read-only (tests) are context: they describe the behaviour that must keep working. Do not return them.

When the task is a provider switch instead (the project's credentials moved from one provider to another, confirmed by \
the developer), the job is to move the code that still calls the old provider onto the new provider's API:
- Read the credential from the new environment variable. Never hard-code a key, and never invent a value for one.
- Use the new provider's real request and response shapes, authentication header and base URL. If a platform is named \
(for example "via AWS"), say in open_questions which client or endpoint you assumed, since that depends on their account setup.
- Keep the application-facing function signatures and return types unchanged, so nothing downstream has to move.
- Choose a model on the new provider that is a reasonable equivalent of the old one, and list that choice in open_questions.
- Test files are editable in this mode only to update test doubles that encode the old provider's wire format. Do not \
weaken or delete assertions about the application's own behaviour.

Return whole files. Only return files you actually changed, and only paths that were given to you."""


def _format_changes(changes: list[dict[str, Any]]) -> str:
    return "\n".join(f"- [{c['severity']}] {c['kind']}: {summarize_change(c)}" for c in changes) or "- (none recorded)"


def build_prompt(*, integration: dict[str, Any], changes: list[dict[str, Any]], docs: dict[str, Any],
                 files: list[dict[str, Any]], previous_attempt: dict[str, Any] | None = None,
                 env_change: dict[str, Any] | None = None, memory: str | None = None,
                 review: dict[str, Any] | None = None, perf: dict[str, Any] | None = None) -> str:
    if perf:
        option, measured = perf["option"], perf["review"]["before"]
        parts = [
            "# Task: relieve a function that is under pressure",
            f"## What was measured\n{perf['review']['label']}: {measured['rps']} calls/s, {measured['service_ms']} ms per call, a budget of "
            f"{measured['budget']} concurrent calls, load {measured['load']}, p95 {measured['p95_ms']} ms.\n{perf['review']['diagnosis']}",
            f"## The change to make\n{option['title']}. {option['summary']}\n"
            + ("Introduce: " + ", ".join(f"`{n['label']}` for calls from `{n['from']}`" for n in option["new_nodes"]) + ".\n" if option["new_nodes"] else "")
            + "Rules for this kind of change: behaviour and public signatures stay the same; no new dependencies; give each call site its own "
              "concurrency bound and a timeout (AbortController); keep error messages as informative as they are; do not touch unrelated code. "
              "If you route to another provider, read its key from the environment binding named in the summary, keep the original path as the "
              "fallback when the new one fails, and map its response into the shape the caller already expects.",
        ]
    elif env_change:
        details = env_change["details"]
        parts = [
            f"# Task: {env_change['kind'].replace('-', ' ')}",
            "## What happened\n"
            f"{env_change['summary']}.\n"
            f"- old environment variable: `{details['from_env']}` (no longer defined)\n"
            f"- new environment variable: `{details['to_env']}`\n"
            f"- old provider: {env_change.get('from_provider') or 'unknown'}"
            + (f" via {details['from_platform']}" if details.get("from_platform") else "") + "\n"
            f"- new provider: {env_change.get('to_provider') or 'unknown'}"
            + (f" via {details['to_platform']}" if details.get("to_platform") else "") + "\n"
            "The developer confirmed this change. The files below still use the old provider or the old variable.",
            "## Changes to apply\n" + _format_changes(changes),
        ]
    else:
        parts = [
            f"# API: {integration['name']} ({integration.get('version') or 'current'})",
            "## Observed contract changes\n" + _format_changes(changes),
        ]
    if memory:
        parts.append("## Your notes on this repository (from earlier work here; private to this repository)\n" + memory)
    if review:
        comments = "\n".join(f"- {c['author']}" + (f" on `{c['path']}`" + (f" line {c['line']}" if c.get("line") else "") if c.get("path") else "")
                             + f": {c['body']}" for c in review["comments"])
        parts.append("## Review feedback on your open pull request\nThe pull request below is already open. A reviewer left these comments. "
                     "Address every one of them; if you disagree with one, keep the code and explain why in open_questions.\n" + comments
                     + "\n\nCurrent diff of the pull request:\n<diff>\n" + review["diff"][-20000:] + "\n</diff>")
    if docs.get("text"):
        parts.append(f"## Provider migration documentation ({docs['url']})\n<documentation>\n{docs['text']}\n</documentation>")
    else:
        fallback = ("Work from your knowledge of the new provider's public API, and list anything you are unsure of in open_questions."
                    if env_change else "Work from the observed changes only, and choose needs_investigation if they do not determine the fix.")
        parts.append(f"## Provider documentation\nNot available ({docs.get('error')}). {fallback}")
    rendered = []
    for f in files:
        note = " read-only" if f.get("read_only") else ""
        rendered.append(f'<file path="{f["path"]}" role="{f["role"]}" distance_from_api_call="{f["depth"]}"{note}>\n{f["content"]}\n</file>')
    parts.append("## Affected files\n" + "\n\n".join(rendered))
    if previous_attempt:
        parts.append(
            "## Your previous attempt failed validation\nThese are the files you returned:\n"
            + json.dumps([f["path"] for f in previous_attempt["files"]])
            + "\n\nValidation output:\n<validation_output>\n" + previous_attempt["output"][-6000:] + "\n</validation_output>\n"
            "Fix the cause. Return the complete set of changed files again, not only the ones you are correcting.")
    return "\n\n".join(parts)


def generate_repair(*, integration: dict[str, Any], changes: list[dict[str, Any]], docs: dict[str, Any],
                    files: list[dict[str, Any]], previous_attempt: dict[str, Any] | None = None,
                    env_change: dict[str, Any] | None = None, memory: str | None = None,
                    review: dict[str, Any] | None = None, perf: dict[str, Any] | None = None) -> RepairResult:
    prompt = build_prompt(integration=integration, changes=changes, docs=docs, files=files, previous_attempt=previous_attempt,
                          env_change=env_change, memory=memory, review=review, perf=perf)
    try:
        result = llm.structured(SYSTEM, prompt, RepairResult)
    except llm.LLMUnavailable as exc:
        raise RepairUnavailable(str(exc)) from exc
    allowed = {f["path"] for f in files if not f.get("read_only")}
    result.files = [f for f in result.files if f.path in allowed]  # never write outside the given set
    return result
