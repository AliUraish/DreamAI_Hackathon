"""Env sensing: classification, the diff that raises a question, and the confirm -> pipeline path."""

import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from app import envwatch

CUSTOMER_APP = Path(__file__).resolve().parents[2] / "demo" / "customer-app"
PROVIDER = "http://localhost:4010"


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    from app import db
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "open_prs", False)
    db.reset_connection()
    yield
    db.reset_connection()


def _snapshot(tmp_path, env_text, name="repo"):
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    (root / ".env").write_text(env_text)
    return envwatch.take_snapshot(root)


def test_classify_by_name_platform_and_value_prefix():
    assert envwatch.classify("AWS_OPENAI_KEY") == {"provider": "openai", "platform": "aws", "by": "name"}
    assert envwatch.classify("ANTHROPIC_API_KEY")["provider"] == "anthropic"
    assert envwatch.classify("LLM_KEY", "sk-ant-abc123") == {"provider": "anthropic", "platform": None, "by": "value-prefix"}
    assert envwatch.classify("DATABASE_URL")["provider"] is None


def test_snapshot_never_contains_the_value(tmp_path):
    secret = "value-that-must-never-be-stored"
    snapshot = _snapshot(tmp_path, f"ANTHROPIC_API_KEY={secret}\nEMPTY_TOKEN=\nUNRELATED=hello\n")
    assert secret not in repr(snapshot) and "must-never-be-stored" not in repr(envwatch.public_view(snapshot))
    assert snapshot["ANTHROPIC_API_KEY"]["fingerprint"] and snapshot["EMPTY_TOKEN"]["fingerprint"] is None
    assert "UNRELATED" not in snapshot  # not credential-like, not tracked


def test_name_and_hash_change_is_a_provider_switch(tmp_path):
    before = _snapshot(tmp_path, "ANTHROPIC_API_KEY=sk-ant-aaaa\n")
    after = _snapshot(tmp_path, "OPENAI_API_KEY=sk-proj-bbbb\n")
    [change] = envwatch.diff_snapshots(before, after)
    assert change["kind"] == "provider-switched" and change["needs_confirmation"]
    assert (change["from_provider"], change["to_provider"]) == ("anthropic", "openai")
    assert change["details"]["value_changed"] is True


def test_openai_to_aws_openai_is_a_switch_of_platform(tmp_path):
    before = _snapshot(tmp_path, "OPENAI_API_KEY=sk-proj-aaaa\n")
    after = _snapshot(tmp_path, "AWS_OPENAI_KEY=ABSKbbbb\n")
    [change] = envwatch.diff_snapshots(before, after)
    assert change["kind"] == "provider-switched"
    assert change["details"]["to_platform"] == "aws" and "via AWS" in change["summary"]


def test_same_value_new_name_is_only_a_rename(tmp_path):
    before = _snapshot(tmp_path, "OPENAI_API_KEY=sk-proj-aaaa\n")
    after = _snapshot(tmp_path, "OPENAI_KEY=sk-proj-aaaa\n")
    [change] = envwatch.diff_snapshots(before, after)
    assert change["kind"] == "env-renamed" and change["details"]["value_changed"] is False


def test_rotation_asks_nothing(tmp_path):
    before = _snapshot(tmp_path, "OPENAI_API_KEY=sk-proj-aaaa\n")
    after = _snapshot(tmp_path, "OPENAI_API_KEY=sk-proj-cccc\n")
    [change] = envwatch.diff_snapshots(before, after)
    assert change["kind"] == "credential-rotated" and not change["needs_confirmation"]


def test_same_name_but_the_key_belongs_to_another_provider(tmp_path):
    before = _snapshot(tmp_path, "LLM_API_KEY=sk-ant-aaaa\n")
    after = _snapshot(tmp_path, "LLM_API_KEY=sk-proj-bbbb\n")
    [change] = envwatch.diff_snapshots(before, after)
    assert change["kind"] == "provider-switched" and change["to_provider"] == "openai"


# --- end to end: sense -> ask -> confirm -> migrate ---------------------------------

OPENAI_CLIENT = '''// Thin client for the LLM that writes customer-facing order notes.
const API_URL = "https://api.openai.com/v1/chat/completions";

export async function complete(prompt: string): Promise<string> {
  const apiKey = process.env.OPENAI_API_KEY;
  if (!apiKey) throw new Error("OPENAI_API_KEY is not set");
  const res = await fetch(API_URL, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${apiKey}` },
    body: JSON.stringify({ model: "gpt-5", messages: [{ role: "user", content: prompt }] }),
  });
  if (!res.ok) throw new Error(`LLM error: ${res.status}`);
  const body = await res.json();
  return body.choices[0].message.content as string;
}
'''


def _needs_demo():
    try:
        up = httpx.get(f"{PROVIDER}/admin/state", timeout=2).is_success
    except httpx.HTTPError:
        up = False
    return not (up and (CUSTOMER_APP / "node_modules").exists())


@pytest.mark.skipif(_needs_demo(), reason="needs the mock provider on :4010 and demo/customer-app installed")
def test_switch_is_sensed_asked_confirmed_and_migrated(tmp_path, monkeypatch, customer_app):
    from app import db, pipeline, service
    from app.repair import PatchedFile, RepairResult

    httpx.post(f"{PROVIDER}/admin/reset")
    repo_dir = customer_app
    (repo_dir / ".env").write_text("ORDERS_API_URL=http://localhost:4010\nANTHROPIC_API_KEY=demo-anthropic-value\n")

    repo = service.connect_repo(local_path=str(repo_dir))
    assert envwatch.check_repo(repo) == []  # nothing changed yet

    # The developer moves to OpenAI: the variable name and the value both change.
    (repo_dir / ".env").write_text("ORDERS_API_URL=http://localhost:4010\nOPENAI_API_KEY=demo-openai-value\n")
    [change] = envwatch.check_repo(repo)
    assert change["status"] == "pending" and change["kind"] == "provider-switched"
    assert envwatch.check_repo(repo) == []  # asked once, not on every tick
    assert db.select("migrations") == []    # and nothing runs before the answer

    question = envwatch.question_for(change)["question"]
    assert "Anthropic" in question["title"] and "OpenAI" in question["title"]

    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        test_file = next(f for f in kwargs["files"] if f["path"] == "tests/assistant.test.ts")
        new_test = (test_file["content"].replace("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
                    .replace('{ content: [{ type: "text", text: "  Hi Ada, your order is on its way!  " }] }',
                             '{ choices: [{ message: { role: "assistant", content: "  Hi Ada, your order is on its way!  " } }] }'))
        return RepairResult(outcome="patched", summary="Moved the assistant to OpenAI.", confirmed_changes=["provider switch confirmed by the user"],
                            open_questions=["Model gpt-5 chosen as the equivalent."],
                            files=[PatchedFile(path="src/lib/assistant.ts", content=OPENAI_CLIENT, reason="OpenAI chat completions"),
                                   PatchedFile(path="tests/assistant.test.ts", content=new_test, reason="test double speaks OpenAI's format"),
                                   PatchedFile(path=".env.example", content="ORDERS_API_URL=http://localhost:4010\nOPENAI_API_KEY=\n", reason="document the new variable")])

    monkeypatch.setattr(pipeline, "generate_repair", fake)
    result = service.confirm_env_change(change["id"])
    assert result["migration_id"]
    pipeline.run_migration(result["migration_id"])
    migration = db.get("migrations", result["migration_id"])

    sent = calls[0]
    paths = {f["path"]: f for f in sent["files"]}
    assert {"src/lib/assistant.ts", "src/services/support.ts", "tests/assistant.test.ts", ".env.example"} <= set(paths)
    assert not any(p.startswith("src/lib/orders") or p == ".env" for p in paths)   # other integrations and real secrets stay out
    assert paths["tests/assistant.test.ts"]["read_only"] is False                  # doubles may be updated in a provider switch
    assert "demo-openai-value" not in repr(sent) and "demo-anthropic-value" not in repr(sent)
    assert sent["env_change"]["details"]["to_env"] == "OPENAI_API_KEY"

    assert migration["validation"]["after"]["passed"] is True, migration["validation"]["after"]
    assert migration["status"] == "ready_local" and migration["trigger"] == "env-change"
    assert "api.openai.com" in migration["diff"] and "Switch LLM provider: Anthropic to OpenAI" == migration["title"]
    assert migration["branch"] == "chowkidaar/anthropic-to-openai"
    assert db.get("env_changes", change["id"])["status"] == "confirmed"
    assert {i["provider"]: i["status"] for i in db.select("integrations")}["openai"] == "pending_code"
    shutil.rmtree(repo_dir, ignore_errors=True)


def test_dismiss_runs_nothing(tmp_path):
    from app import db, service
    repo_dir = tmp_path / "r"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo_dir)], check=True)
    (repo_dir / ".env").write_text("STRIPE_SECRET_KEY=sk_test_aaaa\n")
    subprocess.run(["git", "-C", str(repo_dir), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    repo = service.connect_repo(local_path=str(repo_dir))
    (repo_dir / ".env").write_text("TWILIO_AUTH_TOKEN=bbbb\n")
    [change] = envwatch.check_repo(repo)
    assert service.dismiss_env_change(change["id"])["status"] == "dismissed"
    assert db.select("migrations") == []
    with pytest.raises(ValueError):
        service.confirm_env_change(change["id"])
