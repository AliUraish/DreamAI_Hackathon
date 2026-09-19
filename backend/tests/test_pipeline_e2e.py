"""End-to-end pipeline test with the LLM stubbed out.

Needs the mock provider on :4010 and `npm install` done in demo/customer-app;
skipped otherwise. Everything except the model call is real: drift detection,
Graphify blast radius, working copy, vitest + tsc against the live v2 API, commit.
"""

from pathlib import Path

import httpx
import pytest

PROVIDER = "http://localhost:4010"
CUSTOMER_APP = Path(__file__).resolve().parents[2] / "demo" / "customer-app"

MIGRATED_CLIENT = '''// Client for the Acme Orders API (v2).
const BASE_URL = process.env.ORDERS_API_URL ?? "http://localhost:4010";

export interface Order {
  id: string;
  name: string;
  /** Order value in dollars, e.g. 19.99 */
  price: number;
  status: string;
}

export interface NewOrder {
  name: string;
  price: number;
}

interface OrderV2 {
  id: string;
  customer_name: string;
  /** integer cents */
  amount: number;
  currency: string;
  status: string;
}

function fromApi(order: OrderV2): Order {
  return { id: order.id, name: order.customer_name, price: order.amount / 100, status: order.status };
}

export async function listOrders(): Promise<Order[]> {
  const res = await fetch(`${BASE_URL}/v2/orders`);
  if (!res.ok) throw new Error(`Orders API error: ${res.status}`);
  const body = await res.json();
  return (body.orders as OrderV2[]).map(fromApi);
}

export async function createOrder(input: NewOrder): Promise<Order> {
  const res = await fetch(`${BASE_URL}/v2/orders`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ customer_name: input.name, amount: Math.round(input.price * 100) }),
  });
  if (!res.ok) throw new Error(`Orders API error: ${res.status}`);
  return fromApi((await res.json()) as OrderV2);
}
'''


def _provider_up() -> bool:
    try:
        return httpx.get(f"{PROVIDER}/admin/state", timeout=2).is_success
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(not (_provider_up() and (CUSTOMER_APP / "node_modules").exists()),
                                reason="needs the mock provider on :4010 and demo/customer-app installed")


@pytest.fixture
def backend(tmp_path, monkeypatch):
    from app import db
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "open_prs", False)
    db.reset_connection()
    httpx.post(f"{PROVIDER}/admin/reset")
    yield
    db.reset_connection()
    httpx.post(f"{PROVIDER}/admin/reset")


def _stub(monkeypatch, files):
    from app import pipeline
    from app.repair import PatchedFile, RepairResult
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        return RepairResult(outcome="patched", summary="Moved the client to v2.", confirmed_changes=["price -> amount in cents (docs)"],
                            open_questions=[], files=[PatchedFile(path=p, content=c, reason="v2") for p, c in files.items()])

    monkeypatch.setattr(pipeline, "generate_repair", fake)
    return calls


def _release_and_run(monkeypatch, files, app_path):
    from app import db, service
    from app.pipeline import run_migration
    calls = _stub(monkeypatch, files)
    repo = service.connect_repo(local_path=str(app_path))
    integration = next(i for i in db.select("integrations", {"repo_id": repo["id"]}) if i["provider"] == "acme-orders")
    httpx.post(f"{PROVIDER}/admin/release", json={"mode": "sunset"})
    result = service.check_integration(integration["id"])  # observed drift: nobody told us
    assert result["created"], result
    run_migration(result["migration_id"])
    return db.get("migrations", result["migration_id"]), db.get("integrations", integration["id"]), calls


def test_good_migration_goes_green(backend, monkeypatch, customer_app):
    migration, integration, calls = _release_and_run(monkeypatch, {"src/lib/orders.ts": MIGRATED_CLIENT}, customer_app)
    kinds = {(c["kind"], c.get("before"), c.get("after")) for c in migration["changes"]}
    assert ("endpoint-changed", "/v1/orders", "/v2/orders") in kinds
    assert ("rename-candidate", "name", "customer_name") in kinds
    assert ("rename-candidate", "price", "amount") in kinds

    sent = calls[0]
    assert "integer number of cents" in sent["docs"]["text"]  # the guide reached the model
    assert {f["path"] for f in sent["files"] if not f["read_only"]} == {
        "src/lib/orders.ts", "src/services/billing.ts", "src/services/checkout.ts", "src/routes/orders.ts"}
    assert not any("money.ts" in f["path"] or "health.ts" in f["path"] for f in sent["files"])  # outside the pipeline

    assert migration["validation"]["before"]["passed"] is False  # red on the old code
    assert migration["validation"]["after"]["passed"] is True    # green after the patch
    assert migration["status"] == "ready_local" and migration["kind"] == "repair"
    assert "/v2/orders" in migration["diff"] and "node_modules" not in migration["diff"]
    assert integration["status"] == "migration_ready"
    assert integration["endpoints"][0]["url"].endswith("/v2/orders")  # now tracking the successor


def test_wrong_units_are_caught_and_become_an_investigation(backend, monkeypatch, customer_app):
    # A rename-only migration: right shape, wrong meaning ($1999.00 instead of $19.99).
    naive = MIGRATED_CLIENT.replace("order.amount / 100", "order.amount").replace("Math.round(input.price * 100)", "input.price")
    migration, integration, calls = _release_and_run(monkeypatch, {"src/lib/orders.ts": naive}, customer_app)
    assert len(calls) == 2 and calls[1]["previous_attempt"] is not None  # retried with the failure output
    assert migration["kind"] == "investigation" and migration["status"] == "needs_review"
    assert migration["patched_files"] == [] and "diff --git a/src/" not in migration["diff"]  # no app code shipped
    assert "chowkidaar/acme-orders-v2.md" in migration["diff"]
    assert integration["status"] == "needs_review"
