"""Acme Orders API - the demo API provider.

Starts on v1. `POST /admin/release` ships v2 with three breaking changes:

    /v1/orders -> /v2/orders
    name       -> customer_name
    price      -> amount   (and the unit changes: decimal dollars -> integer cents,
                            which only the migration guide says)

mode="deprecate": v1 keeps working but advertises its successor with the
standard Deprecation / Sunset / Link headers (RFC 8594, RFC 9745).
mode="sunset": v1 answers 410 Gone, so clients still on v1 break.
"""

from __future__ import annotations

import os
import uuid
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

app = FastAPI(title="Acme Orders API", docs_url="/swagger", redoc_url=None)

SEED = [
    {"id": "ord_1", "name": "Grace", "price": 25.5, "status": "paid"},
    {"id": "ord_2", "name": "Linus", "price": 10.0, "status": "paid"},
]

state: dict = {"version": "v1", "v1_mode": "live", "orders": [dict(o) for o in SEED]}

MIGRATION_GUIDE = """# Migrating from Orders API v1 to v2

v1 is deprecated and will be removed. Move every call to v2.

## Breaking changes

1. **Endpoint path.** `/v1/orders` is now `/v2/orders` (same methods: `GET`, `POST`).
2. **`name` is now `customer_name`.** Renamed in both the `POST` request body and every
   order object in responses. The value is unchanged.
3. **`price` is now `amount`, and the unit changed.** `price` was a decimal number of
   dollars (`19.99`). `amount` is an **integer number of cents** (`1999`). Send cents in
   `POST` bodies and expect cents in responses. `POST /v2/orders` rejects non-integer
   amounts with `422`. To show dollars, divide by 100.

## New fields

- `currency` (string, always `"usd"` for now) is added to every order in responses.

## Unchanged

- `id`, `status`, and the response envelope (`{"orders": [...], "total": n}`).

## Example

```http
POST /v2/orders
{"customer_name": "Ada", "amount": 1999}

201 Created
{"id": "ord_3", "customer_name": "Ada", "amount": 1999, "currency": "usd", "status": "pending"}
```
"""


def _cents(price: float) -> int:
    return int(round(price * 100))


def _as_v2(order: dict) -> dict:
    return {
        "id": order["id"],
        "customer_name": order["name"],
        "amount": _cents(order["price"]),
        "currency": "usd",
        "status": order["status"],
    }


def _v1_gate(response: Response) -> None:
    if state["version"] == "v1":
        return
    if state["v1_mode"] == "sunset":
        raise HTTPException(
            status_code=410,
            detail={
                "error": "gone",
                "message": "Orders API v1 has been removed. Use /v2/orders.",
                "successor": "/v2/orders",
                "migration_guide": "/docs/migration/v1-to-v2",
            },
            headers=_deprecation_headers(),
        )
    for key, value in _deprecation_headers().items():
        response.headers[key] = value


def _deprecation_headers() -> dict[str, str]:
    return {
        "Deprecation": "true",
        "Sunset": "Wed, 31 Dec 2026 23:59:59 GMT",
        "Link": '</v2/orders>; rel="successor-version", </docs/migration/v1-to-v2>; rel="deprecation"',
    }


# --- v1 -------------------------------------------------------------------


class NewOrderV1(BaseModel):
    name: str
    price: float


@app.get("/v1/orders")
def list_orders_v1(response: Response):
    _v1_gate(response)
    return {"orders": state["orders"], "total": len(state["orders"])}


@app.post("/v1/orders", status_code=201)
def create_order_v1(body: NewOrderV1, response: Response):
    _v1_gate(response)
    order = {"id": f"ord_{uuid.uuid4().hex[:6]}", "name": body.name, "price": body.price, "status": "pending"}
    state["orders"].append(order)
    return order


# --- v2 -------------------------------------------------------------------


class NewOrderV2(BaseModel):
    customer_name: str
    amount: int  # integer cents; pydantic rejects 19.99 with a 422

    model_config = {"strict": True}


def _require_v2() -> None:
    if state["version"] != "v2":
        raise HTTPException(status_code=404, detail="Not Found")


@app.get("/v2/orders")
def list_orders_v2():
    _require_v2()
    return {"orders": [_as_v2(o) for o in state["orders"]], "total": len(state["orders"])}


@app.post("/v2/orders", status_code=201)
def create_order_v2(body: NewOrderV2):
    _require_v2()
    order = {
        "id": f"ord_{uuid.uuid4().hex[:6]}",
        "name": body.customer_name,
        "price": body.amount / 100,
        "status": "pending",
    }
    state["orders"].append(order)
    return _as_v2(order)


# --- docs + changelog -----------------------------------------------------


@app.get("/docs/migration/v1-to-v2", response_class=PlainTextResponse)
def migration_guide():
    return PlainTextResponse(MIGRATION_GUIDE, media_type="text/markdown")


@app.get("/changelog")
def changelog():
    entries = [{"version": "v1", "date": "2025-03-01", "summary": "Initial release."}]
    if state["version"] == "v2":
        entries.insert(
            0,
            {
                "version": "v2",
                "date": "2026-09-19",
                "breaking": True,
                "summary": "New /v2/orders endpoint; name -> customer_name; price -> amount (integer cents).",
                "migration_guide": "/docs/migration/v1-to-v2",
            },
        )
    return {"entries": entries}


# --- admin (the "Release v2" button) --------------------------------------


class ReleaseRequest(BaseModel):
    mode: Literal["deprecate", "sunset"] = "sunset"
    # Where to announce the release. Defaults to CHOWKIDAAR_WEBHOOK_URL.
    notify_url: str | None = None


@app.post("/admin/release")
async def release_v2(body: ReleaseRequest, request: Request):
    state["version"] = "v2"
    state["v1_mode"] = body.mode
    notify_url = body.notify_url or os.environ.get("CHOWKIDAAR_WEBHOOK_URL")
    notified = None
    if notify_url:
        base = str(request.base_url).rstrip("/")
        payload = {
            "provider": "acme-orders",
            "from_version": "v1",
            "to_version": "v2",
            "successor": {"/v1/orders": "/v2/orders"},
            "docs_url": f"{base}/docs/migration/v1-to-v2",
        }
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                notified = (await client.post(notify_url, json=payload)).status_code
        except httpx.HTTPError as exc:
            notified = f"failed: {exc}"
    return {"version": "v2", "v1_mode": body.mode, "notified": notified}


@app.post("/admin/reset")
def reset():
    state.update(version="v1", v1_mode="live", orders=[dict(o) for o in SEED])
    return {"version": "v1"}


@app.post("/admin/reset-orders")
def reset_orders():
    state["orders"] = [dict(o) for o in SEED]
    return {"ok": True}


@app.get("/admin/state")
def get_state():
    return {"version": state["version"], "v1_mode": state["v1_mode"], "orders": len(state["orders"])}


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException):
    body = exc.detail if isinstance(exc.detail, dict) else {"error": exc.detail}
    return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)
