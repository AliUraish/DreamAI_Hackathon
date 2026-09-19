"""Traffic, the silent audit, the pressure review and its simulated gains, and the route recommendation after an env var is added."""

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import traffic

SHARED_CLIENT = '''const URL = "https://api.openai.com/v1/chat/completions";

async function callOpenAI(body: unknown, key: string) {
  const res = await fetch(URL, { method: "POST", headers: { authorization: `Bearer ${key}` }, body: JSON.stringify(body) });
  return res.json();
}

export async function analyzeImage(image: string) {
  return callOpenAI({ image }, process.env.OPENAI_API_KEY!);
}

export async function writeCaption(text: string) {
  return callOpenAI({ text }, process.env.OPENAI_API_KEY!);
}
'''
ROUTES = '''import { analyzeImage, writeCaption } from "./ai";

export async function postImage(image: string) { return analyzeImage(image); }
export async function postCaption(text: string) { return writeCaption(text); }
'''


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app import db, simrun
    monkeypatch.setattr(simrun.time, "sleep", lambda seconds: traffic.simulator(next(iter(traffic._simulators))).warm(int(seconds) + 30) if traffic._simulators else None)
    from app.config import settings
    from app.main import app
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "open_prs", False)
    db.reset_connection()
    yield TestClient(app)
    for repo_id in list(traffic._simulators):
        traffic.stop_simulation(repo_id)
    db.reset_connection()


@pytest.fixture
def repo(client, tmp_path):
    from app import service
    root = tmp_path / "studio"
    (root / "src").mkdir(parents=True)
    (root / "src" / "ai.ts").write_text(SHARED_CLIENT)
    (root / "src" / "routes.ts").write_text(ROUTES)
    (root / ".env").write_text("OPENAI_API_KEY=sk-proj-aaaa\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "src"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"], check=True)
    return service.connect_repo(local_path=str(root))


def test_queueing_model():
    calm, busy, gone = traffic.response(1, 1000, 6), traffic.response(5.5, 1000, 6), traffic.response(7, 1000, 6)
    assert calm["load"] < 0.2 and calm["wait_ms"] < 1
    assert 0.9 < busy["load"] < 1 and busy["p95_ms"] > 2 * calm["p95_ms"]          # waiting dominates near the budget
    assert gone["saturated"] and gone["capacity_rps"] == 6.0                          # past it there is no steady state
    assert traffic.erlang_c(6, 0) == 0 and traffic.erlang_c(6, 6) == 1


def test_pressure_is_found_reviewed_with_simulated_gains_and_logged_silently(client, repo):
    from app import audit, db, perf
    assert client.post(f"/api/repos/{repo['id']}/traffic/simulate", json={"profile": "steady"}).json()["entries"] == 2
    calm = audit.run_audit(repo, window=40)
    assert calm["verdict"] in {"healthy", "watch"} and calm["action"] is None

    target = client.post(f"/api/repos/{repo['id']}/traffic/simulate", json={"profile": "pressure"}).json()["target"]
    assert target.endswith("callopenai")                                               # the call site two callers share
    traffic.simulator(repo["id"]).warm(60)
    snap = client.get(f"/api/repos/{repo['id']}/traffic?window=40").json()
    assert snap["source"] == "simulated" and snap["nodes"][target]["load"] > perf.PRESSURE_LOAD

    hot, stats = perf.hottest(traffic.snapshot(repo["id"], 40))
    callers = perf.callers_of(traffic.snapshot(repo["id"], 40), hot)
    sim = traffic.simulator(repo["id"])
    slots = {c["node"]: sim.slot_ms(c["node"], hot) for c in callers}
    assert max(slots.values()) > 2 * min(slots.values())                                # the image caller holds a slot far longer
    before, options = perf.build_options(hot, "callOpenAI()", stats, callers, slots, [], {c["node"]: c["node"] for c in callers})
    split = next(o for o in options if o["id"] == "split")
    assert split["after"]["p95_ms"] < before["p95_ms"] and split["gain"]["capacity_pct"] > 50 and len(split["new_nodes"]) == 2

    before_events = len(db.events_after(0))
    judged = audit.run_audit(repo, window=40, act=False)
    assert judged["verdict"] == "pressure" and judged["findings"][0]["label"] == "callOpenAI()"
    assert len([e for e in db.events_after(0) if e["type"] == "ui"]) == len([e for e in db.events_after(0)[:before_events] if e["type"] == "ui"])  # nothing sent to the UI
    log = (Path(db.settings.data_dir) / "audit" / f"{repo['id']}.jsonl").read_text().strip().splitlines()
    assert len(log) == 2 and '"verdict": "pressure"' in log[-1]
    assert [a["verdict"] for a in client.get(f"/api/repos/{repo['id']}/audits").json()][0] == "pressure"


def test_adding_a_provider_key_recommends_a_better_route(client, repo):
    from app import service
    client.post(f"/api/repos/{repo['id']}/traffic/simulate", json={"profile": "pressure"})
    traffic.simulator(repo["id"]).warm(60)
    root = Path(repo["local_path"])
    (root / ".env").write_text("OPENAI_API_KEY=sk-proj-aaaa\nANTHROPIC_API_KEY=sk-ant-bbbb\n")     # the developer adds a second LLM key
    assert service.check_env(repo) == []                                                    # nothing to confirm...
    notes = [n for n in client.get("/api/notifications").json() if n["title"].startswith("Better route available")]
    assert len(notes) == 1 and "Anthropic" in notes[0]["title"] and "Simulated p95" in notes[0]["body"]   # ...but a recommendation, with numbers
    assert client.get(f"/api/repos/{repo['id']}/agent").json()["memory"][0]["content"].startswith("Recommended: Route")
    assert service.check_env(repo) == [] and len([n for n in client.get("/api/notifications").json() if "Better route" in n["title"]]) == 1   # said once


def test_reported_traffic_replaces_simulated(client, repo):
    key = client.post("/api/onboarding", json={"name": "t"}).json()["api_key"]
    client.post(f"/api/repos/{repo['id']}/traffic/simulate", json={"profile": "steady"})
    spans = [{"function": "callOpenAI", "file": "src/ai.ts", "parent": "analyzeImage", "ms": 900, "ok": True} for _ in range(5)]
    assert client.post("/api/v1/traffic", json={"repo_id": repo["id"], "spans": spans}).status_code == 401
    accepted = client.post("/api/v1/traffic", json={"repo_id": repo["id"], "spans": spans + [{"function": "nope", "file": "x.ts", "ms": 1}]},
                           headers={"authorization": f"Bearer {key}"}).json()
    assert accepted == {"accepted": 5, "unmatched": 1}
    snap = client.get(f"/api/repos/{repo['id']}/traffic").json()
    assert snap["source"] == "live" and snap["simulation"] is None and list(snap["links"].values())[0]["mean_ms"] == 900


def test_one_button_simulation_reports_good_bad_and_a_recommendation(client, repo):
    from app import agents, audit, db, simrun
    simrun._running.add(repo["id"])
    assert client.post(f"/api/repos/{repo['id']}/simulation").status_code == 409            # one at a time
    simrun._running.discard(repo["id"])
    assert client.post(f"/api/repos/{repo['id']}/simulation").status_code == 202
    agents.drain(60)
    body = client.get(f"/api/repos/{repo['id']}/simulation").json()
    report = body["report"]
    assert body["running"] is False and report["source"] == "simulated" and report["writtenBy"] == "simulation"   # no LLM key in tests
    assert any("callOpenAI()" in b["text"] for b in report["bad"]) and report["good"]
    rec = report["recommendation"]
    assert rec["label"] == "callOpenAI()" and rec["after"]["p95_ms"] < rec["before"]["p95_ms"] and rec["prefer"]
    assert db.select("migrations") == []                                                       # a what-if opens nothing
    assert traffic.simulator(repo["id"]).profile == "steady"                                   # and it calms down again, so it can be repeated
    assert audit.run_audit(repo, window=10)["verdict"] in {"healthy", "watch", "pressure"}


def test_traffic_keeps_suggesting_where_to_add_a_node(client, repo):
    from app import perf
    client.post(f"/api/repos/{repo['id']}/traffic/simulate", json={"profile": "steady"})
    traffic.simulator(repo["id"]).warm(40)
    assert perf.suggestions(repo, traffic.snapshot(repo["id"], 40)) == []                  # calm: nothing to add
    client.post(f"/api/repos/{repo['id']}/traffic/simulate", json={"profile": "pressure"})
    traffic.simulator(repo["id"]).warm(40)
    [s] = perf.suggestions(repo, traffic.snapshot(repo["id"], 40))
    assert s["label"] == "callOpenAI()" and s["kind"] == "split" and len(s["new_nodes"]) == 2 and s["after"]["p95_ms"] < s["before"]["p95_ms"]


def test_a_replay_knows_when_things_really_happened(client, repo):
    from app import db, ui_events
    for second, t in enumerate(["stage", "release", "change", "done"]):
        db.insert("events", {"id": db.new_id("ui"), "repo_id": repo["id"], "migration_id": "mig_x", "ts": f"2026-09-19T10:00:{0 if second < 2 else second * 7:02d}+00:00",
                             "type": "ui", "message": None, "data": {"t": t}})
    assert [(e["t"], e["offsetMs"]) for e in ui_events.replay("mig_x")] == [("stage", 0), ("release", 500), ("change", 14000), ("done", 21000)]
