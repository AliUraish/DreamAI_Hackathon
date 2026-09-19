# Chowkidaar backend

FastAPI service. Detects API contract changes, maps the code that depends on the API,
generates and validates the migration, opens the pull request.

```
trigger ──► drift check ──► docs ──► affected files ──► reproduce ──► LLM patch ──► validate ──► PR
(webhook     poller.py      docs.py   scanner.py +        validate.py   repair.py     validate.py   gitops.py
 or poll)    schema/                  graph.py (Graphify)
```

## The product in one paragraph

Onboarding creates a workspace and an API key (shown once; only its hash is stored). A project connects by running the connector
inside it; the connector fingerprints credential-like env variables **on the user's machine** (HMAC with a per-workspace salt) and sends
names + fingerprints, never values. Each project gets its own **agent** with private memory (`agent_memory`): it maps the API pipelines,
watches for changes, acts when that is safe and waits for the user when it is not, opens the pull request, addresses review comments
on it, and verifies the base branch after a merge. Agents for different projects run in parallel and share nothing.

| Piece | Where |
|---|---|
| Storage: Neon/Postgres when `NEON_DB` is set, else SQLite. Reads come from an in-memory mirror, writes go to the database in order on a background writer (a query to a hosted Postgres can cost a second; one backend process per database). | `app/db.py` |
| LLM: `OPENAI_KEY` selects OpenAI (`CHOWKIDAAR_OPENAI_MODEL`, default `gpt-5`); Anthropic is the fallback | `app/llm.py` |
| Workspace + API key, connector script and its API (`/api/v1/*`, Bearer key) | `app/workspace.py`, `app/static/connector.py` |
| Agents: one per repo, serial per repo, parallel across repos, private memory | `app/agents.py` |
| Policy: same credential under a new name → handled automatically; different or unknown provider → asks and waits | `app/envwatch.py`, `service.check_env` |
| PR lifecycle: review comments → another round on the same branch; merge → checks on the base branch → `healthy` | `app/prs.py` |
| Explanations of a node or a connection (model-worded from graph facts; graph facts alone without a key) | `app/explain.py` |
| Notifications | `app/notify.py` |

New routes: `POST /api/onboarding`, `GET /api/workspace`, `POST /api/workspace/rotate-key`, `GET /api/v1/connector.py`,
`GET /api/v1/connector/config`, `POST /api/v1/connect`, `POST /api/v1/env`, `GET /api/agents`, `GET /api/repos/{id}/agent`,
`GET /api/notifications`, `POST /api/notifications/read`, `POST /api/explain`. `GET /api/graph?repo_id=` nodes carry `order`
(when the node joins the growing picture) and `status` (`healthy|breaking|affected|patched`). Typed stream additions: `agent`, `notify`,
`map`, `pr.comment`, `pr.updated`, `pr.merged`; every run event carries `repoId` and `runId`.

Tests never touch real credentials: `tests/conftest.py` loads `.env` first, then strips `NEON_DB` / `OPENAI_KEY` for the whole session.
`CHOWKIDAAR_TEST_PG=postgresql://…` runs the suite on a Postgres instead of SQLite.

## Traffic, audits and pressure reviews

- **Traffic** per graph node comes from the project (`POST /api/v1/traffic`, reporter at `GET /api/v1/chowkidaar-traffic.ts`) or from the
  simulator, which generates calls along the project's real call graph (`POST /api/repos/{id}/traffic/simulate` with `steady | pressure | off`).
  Snapshots always say `source: live | simulated`; reported traffic replaces simulated traffic, they are never mixed. The UI gets one snapshot
  per second on the stream (`t: "traffic"`). Model in `app/traffic.py`: load = busy slots / budget, waiting time from M/M/c, API call sites
  default to a budget of 6 concurrent outbound calls.
- **Audit** (`app/audit.py`): every `CHOWKIDAAR_AUDIT_SECONDS` (300) each project's agent judges the whole pipeline from traffic and its
  own log: `healthy | watch | pressure`. It writes `audits` rows and `.data/audit/<repo>.jsonl` and says nothing in the UI. Only a pressure
  verdict starts work. `GET /api/repos/{id}/audits`, `POST /api/repos/{id}/audit`.
- **Pressure review** (`app/perf.py`): measure who sends the traffic, build options (split per caller, route a caller to another provider
  whose key exists, bound with a timeout), run each through the same queueing model, let the model choose and explain, write the change,
  run the project's checks (a check already failing on the base is reported, not gated on; if none pass, no PR), open the PR.
  Numbers in a review are simulation output, never the LLM's. `POST /api/repos/{id}/review {node_id, prefer?}`.
- **A new provider key** of a kind the project already uses (`provider-added` from env sensing) produces a route recommendation with
  simulated numbers (`t: "recommend"`, a notification). Nothing changes until the user applies it.
- PR watching ignores comments from GitHub apps (deploy previews, CI bots). Only files the agent wrote are staged, never build artifacts.

## Run

```bash
cd backend
cp .env.example .env          # set NEON_DB and OPENAI_KEY
uv run uvicorn app.main:app --port 8000
```

Demo provider and an end-to-end run (from the repo root):

```bash
uv run --project backend uvicorn main:app --app-dir demo/provider --port 4010
./demo/run_demo.sh            # provider announces v2 (webhook trigger)
./demo/run_demo.sh poll       # provider ships v2 silently; Chowkidaar notices on its next poll
```

Tests: `uv run pytest` (the end-to-end tests need the provider on :4010; they stub only the LLM call).

Interactive API docs: http://localhost:8000/docs

## What happens without credentials

| Missing | Behaviour |
|---|---|
| `ANTHROPIC_API_KEY` | No code is generated. Chowkidaar opens an **investigation** PR/branch with the evidence instead. |
| GitHub remote on the repo, or a token | The branch is prepared locally (`status: ready_local`); nothing is pushed. |
| `ELEVENLABS_API_KEY` / `NEBIUS_API_KEY` | That integration shows `unmonitored`. |

A real PR needs the connected repo to have a GitHub `origin`. The token comes from
`CHOWKIDAAR_GITHUB_TOKEN`, else `gh auth token`.

## API for the frontend

All JSON. CORS is open. Base: `http://localhost:8000/api`.

| Method | Path | Use |
|---|---|---|
| GET | `/dashboard` | Main view: repos → integrations (status, version, files, latest migration) |
| POST | `/repos` | Connect: `{"local_path": "..."}` or `{"full_name": "owner/repo"}`. Scans and maps integrations. |
| GET / DELETE | `/repos/{id}` | Repo with integrations / disconnect |
| POST | `/repos/{id}/rescan` | Re-run scanner + Graphify |
| GET | `/integrations/{id}` | Detail: call sites (file, line, function, method, path), endpoints, migrations |
| GET | `/integrations/{id}/graph?depth=3` | **Pipeline graph** (below) |
| POST | `/integrations/{id}/check` | Poll now. Starts a migration if breaking drift is found |
| POST | `/check-all` | Poll every monitored integration |
| GET | `/migrations?repo_id=&integration_id=` | Migration cards |
| GET | `/migrations/{id}` | Detail: `steps`, `changes` (with `summary`), `affected_files`, `validation.before/after`, `diff`, `pr_url`, `events` |
| POST | `/migrations/{id}/retry` | Re-run a finished/failed migration |
| GET | `/events?after=<seq>&migration_id=` | Activity feed (history) |
| GET | `/events/stream` | Activity feed (live, Server-Sent Events, event name `activity`) |
| POST | `/webhooks/provider-release` | Provider-initiated trigger; fans out to every repo using that API |
| GET | `/providers` | Known providers |
| GET / POST | `/demo/state`, `/demo/release`, `/demo/reset` | Controls for the Acme Orders mock provider, used by the UI's demo buttons. `release` body: `{"announce": true}` (provider calls our webhook) or `false` (ships silently; a poll has to notice). `reset` also reconnects every repo. |

### The two streaming contracts the UI is built on

| Route | Returns |
|---|---|
| `GET /api/graph` | Graphify `graph.json` shape (NetworkX node-link: `nodes`, `links`) restricted to the maintained API pipelines. Provider nodes have `file_type: "provider"` and `version`; the functions that call them are joined by links with `relation: "calls_api"`. Every node has `community` + `community_name`. Optional `?integration_id=`, `?repo_id=`, `?depth=`. |
| `GET /api/events` with `Accept: text/event-stream` | Unnamed SSE messages, each one `PipelineEvent` from `frontend/src/lib/types.ts` without `at`: `stage`, `release`, `change`, `hit`, `docs`, `excerpt`, `patch.start`, `patch.done`, `check`, `pr`, `done`. `?replay=latest` re-sends the last run first (for a page reload mid-demo). |

The UI (`../frontend`) has no bundled data; it also uses `/dashboard`, `/check-all`, `/repos`, `/migrations`, `/env-changes/*` and `/demo/*`
(table in `frontend/README.md`). Each `change` carries a `label` ("field renamed", "provider switched").

Notes for the UI: node ids are Graphify ids (`src_lib_orders_createorder`), not labels. `change.kind` is `semantic` when the
migration guide's paragraph about a rename talks about units/format - the shape diff cannot see that. `check.phase: "baseline"`
is the unpatched code against the new API (expected red); `"patched"` goes `running` → `passed`/`failed`. `pr` is only sent
for a real GitHub PR; a local-only run ends with `done` and a message. The same events are on `GET /api/migrations/{id}` as `ui_events`.

Without `Accept: text/event-stream`, `GET /api/events` returns the plain activity feed as JSON.

### Environment sensing: "did you switch providers?"

The backend re-reads each connected repo's env files every `CHOWKIDAAR_ENV_WATCH_SECONDS` (default 5) and the env vars its
code reads. A credential's **name** says which provider it is (`AWS_OPENAI_KEY` = OpenAI via AWS); the **hash of its value**
says whether it is still the same credential.

| What changed | Result |
|---|---|
| name gone + new name + different hash, different provider or platform | `provider-switched` → **asks the user** |
| name gone + new name, same provider (or same hash) | `env-renamed` → **asks the user** |
| same name, new hash, but the key's prefix belongs to another provider | `provider-switched` → **asks the user** |
| same name, new hash, same provider | `credential-rotated` → activity feed only |
| new provider's variable added / a variable removed | `provider-added` / `credential-removed` → activity feed only |

Nothing runs until the user answers. On confirm, the code that still uses the old provider or the old variable goes through
the normal pipeline (affected files → patch → checks → PR) with `trigger: "env-change"`.

| Method | Path | Use |
|---|---|---|
| GET | `/env-changes?status=pending&repo_id=` | Questions waiting for the user. Each has `question: {title, body, needs_provider_choice, provider_options, actions}` |
| POST | `/env-changes/{id}/confirm` | Body optional: `{"to_provider": "openai"}` when `needs_provider_choice` is true. Returns `{env_change, migration_id}` (202) |
| POST | `/env-changes/{id}/dismiss` | Nothing runs |
| GET | `/repos/{id}/env` | Tracked variables: name, provider, platform, 8-char fingerprint, files, `referenced_in`. **Never values.** |
| POST | `/repos/{id}/env/check` | Re-read now instead of waiting for the watcher |

`GET /dashboard` also carries `pending_confirmations`. For a prompt in the graph UI, the typed SSE stream (`GET /api/events`)
sends two extra event types; clients that do not know them can ignore them (both carry `msg` for the feed):

```jsonc
{"t": "confirm.request", "msg": "Needs your confirmation · ...", "request": {
   "id": "env_ab12", "kind": "provider-switched", "title": "Switch from Anthropic to OpenAI?", "body": "...",
   "fromProviderId": "provider:anthropic", "toProviderId": "provider:openai", "needsProviderChoice": false,
   "actions": {"confirm": "/api/env-changes/env_ab12/confirm", "dismiss": "/api/env-changes/env_ab12/dismiss"}}}
{"t": "confirm.resolved", "id": "env_ab12", "decision": "confirmed" | "dismissed", "msg": "..."}
```

After `confirmed`, the usual run follows on the same stream (`stage`, `release` with
`source: "environment change confirmed by the user · X → Y"`, `change` cards for the provider and the variable, ...).

Secrets: a value is read only to compute a keyed HMAC (key is random per install, in `.data/env_hmac.key`) and to look at
a well-known key prefix. Values are never stored, logged, returned by the API, or sent to the LLM; real env files are never
sent to the LLM either (`.env.example` is, only when none of its lines holds a secret). `.env.example` lagging behind `.env`
does not hide a change: real env files decide what is active.

In a provider switch, test files become editable so test doubles of the old wire format can be updated; the PR body lists
any test file that was touched under "Please double check".

Demo: `./demo/run_env_demo.sh` (confirm) or `./demo/run_env_demo.sh dismiss`. It uses fake keys in `demo/customer-app/.env`.

### Statuses

- integration: `healthy` · `deprecated` · `breaking` · `migrating` · `migration_ready` · `needs_review` · `unmonitored` · `pending_code` (new provider confirmed, code not moved yet)
- env change: `pending` · `confirmed` · `dismissed` · `superseded`
- migration: `queued` · `running` · `pr_opened` · `ready_local` · `needs_review` · `failed`; `kind`: `repair` | `investigation`
- migration step: `pending` · `running` · `done` · `failed` · `skipped`; keys in order:
  `detect`, `docs`, `affected`, `reproduce`, `patch`, `validate`, `pr`
- change `kind`: `endpoint-gone`, `endpoint-changed`, `endpoint-deprecated`, `field-removed`, `field-added`,
  `type-changed`, `became-nullable`, `required-to-optional`, `optional-to-required`, `rename-candidate`;
  `severity`: `BREAKING` | `WARNING` | `INFO`

### Pipeline graph

`GET /integrations/{id}/graph` returns only the maintained API's pipeline, never the whole
codebase. Graphify builds the full code graph internally; the response is the slice reachable
from the API call sites within `depth` hops. `stats.excluded_nodes` says how much was left out.

```jsonc
{
  "nodes": [
    {"id": "provider:acme-orders", "kind": "provider", "label": "Acme Orders", "status": "breaking", "version": "v1", "depth": -2},
    {"id": "endpoint:GET /v1/orders", "kind": "endpoint", "method": "GET", "path": "/v1/orders", "status": "breaking", "depth": -1},
    {"id": "src_lib_orders", "kind": "file", "label": "lib/orders.ts", "file": "src/lib/orders.ts", "line": 1,
     "depth": 0, "role": "call_site", "status": "affected"},
    {"id": "src_services_billing_revenuetotal", "kind": "symbol", "label": "revenueTotal()", "file": "src/services/billing.ts",
     "line": 5, "depth": 1, "role": "dependent", "status": "affected"}
  ],
  "edges": [
    {"source": "provider:acme-orders", "target": "endpoint:GET /v1/orders", "relation": "exposes"},
    {"source": "src_lib_orders_listorders", "target": "endpoint:GET /v1/orders", "relation": "calls_api", "file": "src/lib/orders.ts", "line": 18},
    {"source": "src_services_billing_revenuetotal", "target": "src_lib_orders_listorders", "relation": "calls", "confidence": "EXTRACTED"}
  ],
  "stats": {"nodes": 17, "edges": 25, "files": 5, "repo_nodes_total": 43, "excluded_nodes": 29},
  "files": ["src/lib/orders.ts", "..."]
}
```

- `kind`: `provider` | `endpoint` | `file` | `symbol`
- `depth`: -2 provider, -1 endpoint, 0 the API call site, n = hops downstream. Good for a left-to-right layout.
- `status`: `healthy` | `breaking` | `affected` | `patched` (re-fetch after `migration.patch.done` to see files turn `patched`)
- `relation`: `exposes`, `calls_api`, `calls`, `imports_from`, `contains`, `inherits`, ...

### Live feed

```js
const feed = new EventSource("http://localhost:8000/api/events/stream");
feed.addEventListener("activity", (e) => {
  const { ts, type, message, migration_id, data } = JSON.parse(e.data);
});
```

`type` values: `repo.connected`, `integration.mapped`, `release.announced`, `drift.detected`,
`check.completed`, `migration.<step>.<done|failed|skipped>`, `migration.completed`, `migration.failed`,
`env.change.detected`, `env.change.confirmed`, `env.change.dismissed`, `env.credential-rotated`, `env.provider-added`, `env.credential-removed`.

## Design notes

- **Schema engine** (`app/schema/`) is a Python port of `@schema-watch/core` (Apache-2.0, see `NOTICE`) plus
  rename-candidate detection. A changed hash with an empty diff is not drift.
- **Drift signals**: response shape, `Deprecation`/`Sunset` headers, `Link rel="successor-version"`, 404/410.
  When a successor is advertised, the diff is baseline(v1) vs live(v2).
- Third-party list endpoints (ElevenLabs, Nebius) only alert on `field-removed` / `type-changed`, because
  optionality there follows the data.
- **Probes are GET-only.** Chowkidaar never calls a mutating endpoint.
- **Tests are read-only context** for the LLM; it cannot edit them to make them pass.
- The LLM may only write paths it was given. Validation failure → one retry with the output → investigation PR
  with no application code changed.
- Migrations run in a throwaway clone under `.data/work/`; the connected checkout is never touched.
