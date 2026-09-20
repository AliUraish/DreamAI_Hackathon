"""Onboarding, the connector API, per-repo agents, explanations, and the pull request lifecycle."""

import hashlib
import hmac
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

CUSTOMER_APP = Path(__file__).resolve().parents[2] / "demo" / "customer-app"
PROVIDER = "http://localhost:4010"


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app import db
    from app.config import settings
    from app.main import app
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "open_prs", False)
    db.reset_connection()
    yield TestClient(app)
    db.reset_connection()


def _repo(tmp_path, name, env_text, source='import Stripe from "stripe";\nexport const stripe = new Stripe(process.env.STRIPE_SECRET_KEY);\n'):
    root = tmp_path / name
    (root / "src").mkdir(parents=True)
    (root / "src" / "pay.ts").write_text(source)
    (root / ".env").write_text(env_text)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "src"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"], check=True)
    return root


def _fp(key, value):
    return hmac.new(key.encode(), value.encode(), hashlib.sha256).hexdigest()[:16]


def test_onboarding_gives_a_key_once_and_the_connector_needs_it(client, tmp_path):
    from app import agents, db
    assert client.get("/api/workspace").json() == {"onboarded": False}
    created = client.post("/api/onboarding", json={"name": "Acme"}).json()
    key = created["api_key"]
    assert key.startswith("ck_live_") and key in created["connect_command"]
    assert client.post("/api/onboarding", json={"name": "again"}).status_code == 409
    assert "api_key" not in client.get("/api/workspace").json()            # never shown again
    assert key not in repr(db.select("workspaces"))                          # only its hash is stored

    assert client.get("/api/v1/connector/config").status_code == 401
    assert client.post("/api/v1/connect", json={"local_path": "/x"}, headers={"authorization": "Bearer ck_live_wrong"}).status_code == 401
    assert "CHOWKIDAAR_API_KEY" in client.get("/api/v1/connector.py").text

    root = _repo(tmp_path, "shop", "STRIPE_SECRET_KEY=sk_test_aaaa\n")
    auth = {"authorization": f"Bearer {key}"}
    salt = client.get("/api/v1/connector/config", headers=auth).json()["fingerprint_salt"]
    rotated = client.post("/api/workspace/rotate-key").json()["api_key"]              # a rotation changes the key, not the fingerprints
    assert client.get("/api/v1/connector/config", headers=auth).status_code == 401
    auth = {"authorization": f"Bearer {rotated}"}
    assert client.get("/api/v1/connector/config", headers=auth).json()["fingerprint_salt"] == salt
    key = salt
    body = {"local_path": str(root), "remote_url": None, "branch": "main",
            "env": [{"name": "STRIPE_SECRET_KEY", "fingerprint": _fp(key, "sk_test_aaaa"), "files": [".env"], "hint": "stripe"}]}
    repo_id = client.post("/api/v1/connect", json=body, headers=auth).json()["repo_id"]
    agents.drain()
    env = client.get(f"/api/repos/{repo_id}/env").json()["variables"]
    assert [(v["name"], v["provider"], v["has_value"]) for v in env] == [("STRIPE_SECRET_KEY", "stripe", True)]
    assert "sk_test_aaaa" not in repr(db.select("env_snapshots"))            # the server never saw the value
    assert "pipeline" in {m["kind"] for m in client.get(f"/api/repos/{repo_id}/agent").json()["memory"]}

    # Same credential, new name: safe, so the agent acts without asking.
    body["env"] = [{"name": "STRIPE_KEY", "fingerprint": _fp(key, "sk_test_aaaa"), "files": [".env"], "hint": "stripe"}]
    pushed = client.post("/api/v1/env", json={"repo_id": repo_id, "env": body["env"]}, headers=auth).json()["changes"]
    assert "handled automatically" in pushed[0]
    assert client.get("/api/env-changes").json() == []
    # A different provider: the agent waits for the user.
    body["env"] = [{"name": "PAYPAL_TOKEN", "fingerprint": _fp(key, "zzzz"), "files": [".env"], "hint": None}]
    pushed = client.post("/api/v1/env", json={"repo_id": repo_id, "env": body["env"]}, headers=auth).json()["changes"]
    assert "waiting for you" in pushed[0]
    agents.drain()
    dashboard = client.get("/api/dashboard").json()
    assert dashboard["pending"][0]["needsProviderChoice"] is True
    assert [a["status"] for a in dashboard["agents"]] == ["waiting"]
    assert any(n["level"] == "action" for n in client.get("/api/notifications").json())


def test_each_repository_has_its_own_agent_and_memory(client, tmp_path):
    from app import agents, service
    a = service.connect_repo(local_path=str(_repo(tmp_path, "alpha", "STRIPE_SECRET_KEY=sk_test_a\n")))
    b = service.connect_repo(local_path=str(_repo(tmp_path, "beta", "TWILIO_AUTH_TOKEN=bbbb\n", 'import twilio from "twilio";\nexport const t = twilio("AC", process.env.TWILIO_AUTH_TOKEN);\n')))
    agents.remember(a["id"], "convention", "alpha uses cents everywhere")
    assert "alpha uses cents" in agents.recall(a["id"]) and "alpha" not in agents.recall(b["id"])
    assert {x["repoName"] for x in client.get("/api/agents").json()} == {"alpha", "beta"}
    client.delete(f"/api/repos/{a['id']}")
    assert agents.recall(a["id"]) is None and agents.recall(b["id"])


def _needs_demo():
    try:
        up = httpx.get(f"{PROVIDER}/admin/state", timeout=2).is_success
    except httpx.HTTPError:
        up = False
    return not (up and (CUSTOMER_APP / "node_modules").exists())


@pytest.mark.skipif(_needs_demo(), reason="needs the mock provider on :4010 and demo/customer-app installed")
def test_explain_graph_order_and_red_marking(client, customer_app):
    from app import db, service
    httpx.post(f"{PROVIDER}/admin/reset")
    repo = service.connect_repo(local_path=str(customer_app))
    graph = client.get(f"/api/graph?repo_id={repo['id']}").json()
    orders = {n["label"]: n["order"] for n in graph["nodes"]}
    assert orders["Acme Orders"] == 0 and orders["createOrder()"] == 1 and orders["placeOrder()"] == 2   # grows outwards from the API
    assert {n["status"] for n in graph["nodes"]} == {"healthy"}

    node = client.post("/api/explain", json={"repo_id": repo["id"], "node_id": "src_lib_orders_createorder"}).json()
    assert "POST /v1/orders" in node["text"] and node["source"] == "graph"          # no LLM key: the graph facts answer
    link = client.post("/api/explain", json={"repo_id": repo["id"], "source": "src_services_checkout_placeorder", "target": "src_lib_orders_createorder"}).json()
    assert "calls" in link["text"] and "checkout.ts" in link["text"]
    quick = client.post("/api/explain", json={"repo_id": repo["id"], "node_id": "src_lib_orders_createorder", "mode": "facts"}).json()
    assert quick["source"] == "graph" and quick["ai_available"] is False and quick["cached"] is False   # facts are never cached: they must not block a later AI answer
    assert client.get(f"/api/repos/{repo['id']}/explanations").json()["written"] == 0

    integration = next(i for i in db.select("integrations", {"repo_id": repo["id"]}) if i["provider"] == "acme-orders")
    db.update("integrations", integration["id"], {"status": "breaking"})
    red = {n["label"]: n["status"] for n in client.get(f"/api/graph?repo_id={repo['id']}").json()["nodes"]}
    assert red["Acme Orders"] == "breaking" and red["createOrder()"] == "breaking" and red["placeOrder()"] == "affected"
    assert red["complete()"] == "healthy"                                                 # the Anthropic pipeline is untouched


def test_pull_request_lifecycle_review_then_merge(client, tmp_path, monkeypatch):
    """GitHub is stubbed; everything the agent does around it is real (files, checks, memory, events)."""
    from app import agents, db, gitops, pipeline, prs, service
    from app.repair import PatchedFile, RepairResult

    root = _repo(tmp_path, "shop", "STRIPE_SECRET_KEY=sk_test_aaaa\n")
    repo = service.connect_repo(local_path=str(root))
    db.update("repos", repo["id"], {"full_name": "acme/shop", "validation_commands": [{"name": "tests", "cmd": "test -f src/pay.ts"}]})
    integration = db.select("integrations", {"repo_id": repo["id"]})[0]
    migration = db.insert("migrations", {"id": "mig_1", "repo_id": repo["id"], "integration_id": integration["id"], "title": "Migrate Stripe", "status": "pr_opened",
                                         "kind": "repair", "trigger": "poll", "changes": [], "affected_files": [{"path": "src/pay.ts", "depth": 0, "role": "call_site", "read_only": False}],
                                         "steps": [], "validation": {}, "patched_files": ["src/pay.ts"], "meta": {}, "branch": "chowkidaar/stripe", "pr_url": "https://github.com/acme/shop/pull/7",
                                         "pr_number": 7, "pr_state": "open", "review": {"seen": [], "rounds": []}, "created_at": db.now(), "updated_at": db.now()})
    db.update("integrations", integration["id"], {"status": "migration_ready"})

    state = {"state": "open", "merged_at": None, "head_sha": "abc", "merge_commit": None, "base": "main", "url": migration["pr_url"]}
    comments = [{"id": "review:1", "author": "ali", "body": "Please read the key through a helper.", "path": "src/pay.ts", "line": 1, "at": "2026-09-19T00:00:00Z", "reply_to": 1}]
    replies, pushed = [], []

    def fake_checkout(full_name, branch, target, deps_from=None):
        subprocess.run(["git", "clone", "--quiet", str(root), str(target)], check=True)
        return target

    monkeypatch.setattr(gitops, "pull_request", lambda full_name, number: state)
    monkeypatch.setattr(gitops, "pull_request_comments", lambda full_name, number: comments)
    monkeypatch.setattr(gitops, "comment_on_pull_request", lambda full_name, number, body, reply_to=None: replies.append((body, reply_to)))
    monkeypatch.setattr(gitops, "push_branch", lambda workdir, full_name, branch: pushed.append(branch))
    monkeypatch.setattr(gitops, "checkout_remote_branch", fake_checkout)
    seen_prompts = []

    def fake_repair(**kwargs):
        seen_prompts.append(kwargs)
        return RepairResult(outcome="patched", summary="Key now read through getStripeKey().", confirmed_changes=[], open_questions=[],
                            files=[PatchedFile(path="src/pay.ts", content="export const getStripeKey = () => process.env.STRIPE_SECRET_KEY;\n", reason="review")])

    monkeypatch.setattr(prs, "generate_repair", fake_repair)

    prs.watch_once(); agents.drain()
    assert seen_prompts[0]["review"]["comments"][0]["body"].startswith("Please read the key")
    assert "Reviewer ali" in seen_prompts[0]["memory"]                                   # the agent's own notes reach the prompt
    assert pushed == ["chowkidaar/stripe"] and replies[0][1] == 1 and "Done in" in replies[0][0]
    after = db.get("migrations", "mig_1")
    assert after["review"]["seen"] == ["review:1"] and len(after["review"]["rounds"]) == 1

    prs.watch_once(); agents.drain()
    assert len(seen_prompts) == 1                                                        # the same comment is not handled twice

    state.update(state="merged", merged_at="2026-09-19T01:00:00Z")
    prs.watch_once(); agents.drain()
    assert db.get("migrations", "mig_1")["status"] == "merged"
    assert db.get("integrations", integration["id"])["status"] == "healthy"
    titles = [n["title"] for n in client.get("/api/notifications").json()]
    assert any("merged and verified" in t for t in titles) and any("addressed" in t for t in titles)
    assert [a["status"] for a in client.get("/api/agents").json()] == ["idle"]


def test_concurrent_requests_on_a_cold_cache_build_the_graph_once(client, tmp_path, monkeypatch):
    """After a restart the UI fires several requests at once; they must not race on Graphify's output directory."""
    from concurrent.futures import ThreadPoolExecutor
    from app import service
    repo = service.connect_repo(local_path=str(_repo(tmp_path, "shop", "STRIPE_SECRET_KEY=sk_test_aaaa\n")))
    service._analysis_cache.clear()
    builds = []
    real = service.build_code_graph
    monkeypatch.setattr(service, "build_code_graph", lambda *a, **k: (builds.append(k.get("force")), real(*a, **k))[1])
    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(lambda _: client.get(f"/api/graph?repo_id={repo['id']}").status_code, range(8)))
    assert codes == [200] * 8 and builds == [False]      # one build, from the graph on disk, nobody got a 500


def test_comments_from_apps_are_not_review_feedback(monkeypatch):
    """Found on a real PR: Cloudflare's deploy bot commented, and the agent answered it as if a reviewer had spoken."""
    from app import gitops
    pages = {
        "/repos/a/b/issues/7/comments": [
            {"id": 1, "user": {"login": "cloudflare-workers-and-pages[bot]", "type": "Bot"}, "body": "## Deploying with Cloudflare Workers", "created_at": "2026-09-19T00:00:00Z"},
            {"id": 2, "user": {"login": "ali", "type": "User"}, "body": "Please add a timeout.", "created_at": "2026-09-19T00:01:00Z"},
            {"id": 3, "user": {"login": "ali", "type": "User"}, "body": "Done in abc. " + gitops.BOT_MARKER, "created_at": "2026-09-19T00:02:00Z"}],
        "/repos/a/b/pulls/7/comments": [{"id": 4, "user": {"login": "vercel[bot]", "type": "Bot"}, "body": "Preview ready", "created_at": "2026-09-19T00:03:00Z", "path": "x.ts"}],
        "/repos/a/b/pulls/7/reviews": [{"id": 5, "user": {"login": "github-actions[bot]", "type": "Bot"}, "body": "CI summary", "submitted_at": "2026-09-19T00:04:00Z"}],
    }
    monkeypatch.setattr(gitops, "_github", lambda method, path, **kwargs: pages[path])
    assert [(c["author"], c["body"]) for c in gitops.pull_request_comments("a/b", 7)] == [("ali", "Please add a timeout.")]


def test_resetting_the_demo_never_touches_other_projects(client, tmp_path, monkeypatch):
    """A real project's record was once lost to a demo reset that disconnected everything."""
    from app import demo, service
    from app.routers import api
    monkeypatch.setattr(api, "_acme", lambda *a, **k: {"version": "v1"})
    monkeypatch.setattr(demo, "materialize", lambda target=None: _repo(tmp_path, "demo-app", "ORDERS_API_URL=http://localhost:4010\n") if not (tmp_path / "demo-app").exists() else tmp_path / "demo-app")
    real = service.connect_repo(local_path=str(_repo(tmp_path, "real-project", "STRIPE_SECRET_KEY=sk_test_aaaa\n")))
    service.connect_repo(local_path=str(demo.materialize()))
    assert client.post("/api/demo/reset").status_code == 200
    names = {r["name"]: r["id"] for r in client.get("/api/repos").json()}
    assert names["real-project"] == real["id"] and "demo-app" in names          # same id: it was never disconnected


def test_open_pull_requests_are_adopted_from_github_once(client, tmp_path, monkeypatch):
    """The database can be new while the pull requests are not. GitHub is the record; nothing about them is made up."""
    from app import db, gitops, prs, service, ui_events
    repo = service.connect_repo(local_path=str(_repo(tmp_path, "shop", "STRIPE_SECRET_KEY=sk_test_aaaa\n")))
    repo = db.update("repos", repo["id"], {"full_name": "acme/shop"}) or db.get("repos", repo["id"])
    diff = "diff --git a/src/pay.ts b/src/pay.ts\n--- a/src/pay.ts\n+++ b/src/pay.ts\n@@ -1,1 +1,2 @@\n import Stripe from \"stripe\";\n+// queue\n"
    found = [{"number": 7, "title": "Relieve pressure on charge()", "body": "## why", "url": "https://github.com/acme/shop/pull/7",
              "branch": "chowkidaar/relieve-pay-ts-route", "base": "main", "created_at": "2026-09-19T10:00:00Z", "diff": diff}]
    monkeypatch.setattr(gitops, "github_token", lambda: "token")
    monkeypatch.setattr(gitops, "open_pull_requests_by_prefix", lambda full_name, prefix: found)
    monkeypatch.setattr(gitops, "pull_request_comments", lambda full_name, number: [{"id": 41, "body": "old", "user": "ali"}])

    (migration_id,) = prs.adopt(repo)
    migration = db.get("migrations", migration_id)
    assert (migration["pr_number"], migration["pr_state"], migration["status"], migration["kind"]) == (7, "open", "pr_opened", "performance")
    assert (migration["meta"]["label"], migration["meta"]["prefer"]) == ("pay.ts", "route")   # so the same review is not opened again
    assert migration["review"]["seen"] == [41]                                     # an old comment does not start a new round
    assert [e["t"] for e in ui_events.replay(migration_id)] == ["stage", "patch.done", "stage", "pr", "done"]
    assert client.get("/api/dashboard").status_code == 200 and client.get(f"/api/migrations/{migration_id}").status_code == 200
    assert migration in prs.open_pull_requests() and prs.adopt(repo) == []         # watched from now on, and never adopted twice
