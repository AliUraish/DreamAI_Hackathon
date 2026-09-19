# Chowkidaar frontend

The API pipeline, shown on the repo's code graph. Vite + React + TypeScript, a custom canvas renderer over `d3-force`.

```bash
npm install
npm run dev        # http://localhost:5173
```

There is no bundled data. Everything on screen comes from the Chowkidaar backend (`../backend`, FastAPI on :8000, proxied
under `/api`). With the backend down the page says so and reconnects by itself.

```bash
# from the repo root, in two more terminals
uv run --project backend uvicorn app.main:app --app-dir backend --port 8000
uv run --project backend uvicorn main:app --app-dir demo/provider --port 4010     # the demo API provider
```

## Shape of the app

No player: runs are driven by the agents, the UI shows where they stand. First run is onboarding (workspace → API key, shown once →
connect command). Each project has its own graph, runs and agent; the project switcher is in the top bar and "Agents working" sits
top right. A project's graph is not shown at once: it grows from the API outwards (`order` from the backend) every time the project
is opened. Click a node **or a connection** and the project's agent explains it. Open issues are red/amber on the graph even with no
run on screen (`status` from the backend). The PR stage shows review comments, the agent's fix commits, and the verified merge.

## Driving it

| | |
|---|---|
| `/` | find a node |
| `F` | fit the graph · `esc` deselect |

On the canvas: drag to pan, scroll or pinch to zoom, drag a node to move it, click to inspect, double-click to zoom into its neighbourhood. Hovering a breaking change, a file row, a doc excerpt, a test or an integration lights the matching nodes; hovering a traced file lights its path back to the API. Panning or zooming hands you the camera; the crosshair button gives it back to the pipeline.

## How it works

A run is an append-only list of `PipelineEvent`s (`src/lib/types.ts`). The whole UI, canvas included, is a pure function of `(events, clock)` (`src/lib/derive.ts`), which is why the rail can scrub backwards and every animation replays identically. The live stream and a replayed past run feed the same log.

```
src/lib        types, the event reducer, the zustand store (clock, selection, camera requests)
src/data       api.ts (typed backend client), source.ts (graph + event stream + dashboard polling)
src/graph      layout.ts (d3-force), render.ts (canvas drawing), GraphCanvas.tsx (input, camera, frame loop)
src/ui         stage panel, inspector, pipeline rail, integrations, activity feed, search, confirm prompt
```

## What the UI does with the backend

| In the UI | Backend |
|---|---|
| Graph | `GET /api/graph`: only the maintained API pipelines, never the whole repo |
| A run playing on the rail | `GET /api/events` (SSE), one `PipelineEvent` per message |
| Integrations list, statuses, "polled 2m ago" | `GET /api/dashboard`, every 5 s and after each run |
| **"Switch from Anthropic to OpenAI?"** prompt | `confirm.request` / `confirm.resolved` on the stream, `pending_confirmations` on the dashboard; the buttons call `POST /api/env-changes/{id}/confirm` or `/dismiss`. Nothing runs before the answer. |
| Poll contracts now | `POST /api/check-all` |
| Connect a repository (shown when none is connected) | `POST /api/repos` with `owner/repo` or a local path |
| Last run: replay | `GET /api/migrations`, then `ui_events` of `GET /api/migrations/{id}` |
| Demo provider: ship v2, ship silently then poll, reset | `POST /api/demo/release`, `/api/demo/reset`; hidden unless the demo provider is up and the repo calls it |
| `live` / `backend offline · retrying` in the top bar | state of the event stream |

A reload in the middle of a run resumes it (the page asks for `?replay=latest` when the newest migration is still running);
`/?replay` forces that.

## The two contracts

**`GET /api/graph`** returns graphify's `graph.json` as is (node-link: `nodes[{id, label, community, community_name, file_type, source_file, source_location}]`, `links[{source, target, relation}]`). Graphify does not model outbound HTTP calls, so the backend adds:

- one node per provider: `{ "id": "provider:orders-api", "label": "Orders API", "file_type": "provider", "version": "v1", "community": 0 }`
- one link per call site: `{ "source": "<function or file id>", "target": "provider:orders-api", "relation": "calls_api" }`

**`GET /api/events`** is a `text/event-stream`. Each message's `data` is one `PipelineEvent` as JSON, without `at` (the frontend stamps arrival time). In order:

```jsonc
{ "t": "stage", "stage": "detect" }
{ "t": "release", "providerId": "provider:orders-api", "from": "v1", "to": "v2", "source": "openapi.yaml changed", "msg": "API release detected" }
{ "t": "stage", "stage": "diff" }
{ "t": "change", "change": { "id": "name", "kind": "request", "label": "field renamed", "before": "name", "after": "customer_name", "where": "POST body", "nodeIds": ["..."] } }
{ "t": "stage", "stage": "trace" }
{ "t": "hit", "hit": { "nodeId": "createOrder()", "from": "provider:orders-api", "hop": 1, "role": "symbol" } }   // role: change | symbol | dependent | test
{ "t": "stage", "stage": "docs" }
{ "t": "docs", "title": "...", "url": "..." }
{ "t": "excerpt", "excerpt": { "id": "d1", "section": "...", "text": "...", "changeIds": ["name"], "nodeIds": ["..."] } }
{ "t": "stage", "stage": "patch" }
{ "t": "patch.start", "nodeId": "lib/orders-client.ts" }
{ "t": "patch.done", "patch": { "nodeId": "...", "path": "...", "additions": 9, "deletions": 3, "hunks": [{ "header": "@@ ...", "lines": [{ "t": "+", "s": "..." }] }] } }
{ "t": "stage", "stage": "verify" }
{ "t": "check", "check": { "id": "t1", "group": "tests", "name": "...", "phase": "baseline", "status": "failed", "detail": "...", "nodeId": "tests/..." } }
{ "t": "check", "check": { "id": "t1", "group": "tests", "name": "...", "phase": "patched", "status": "passed" } }   // group: tests | types | build
{ "t": "stage", "stage": "pr" }
{ "t": "pr", "pr": { "number": 12, "title": "...", "branch": "...", "base": "main", "url": "https://github.com/...", "files": 4, "additions": 21, "deletions": 8 } }
{ "t": "done" }
```

Things the adapter does that are easy to trip over:

- `graph.repo` and `graph.repo_nodes_total` from the payload name the repo and label the graph as a pipeline slice of N nodes.
- Events are stamped on arrival but never closer together than a minimum pace (`paceAfter` in `src/lib/store.ts`), so a graph walk that arrives as one burst still plays out hop by hop.
- A second `stage: detect` starts a fresh run, and the graph is refetched first, because the demo script reconnects the repo. While the graph is empty the page keeps polling for one.
- A run that sends `done` without `pr` shows its last `msg` as the reason, marks the stage it stopped in on the rail, and the integration reads "needs review". A `patch.done` with no hunks does not turn anything green.

`nodeId`, `from` and `nodeIds` must be ids from `/api/graph`; every `hit` needs a link between `from` and `nodeId` (either direction) for its edge label. Any event may carry `msg`, which becomes a line in the activity feed. Dependents turn green once a `build` check passes in the `patched` phase.

`change.label` is the backend's name for what happened ("field renamed", "renamed + meaning changed", "provider switched");
without it the UI falls back to a label per `kind`. Two more message types share the stream and are not part of a run's log:

```jsonc
{ "t": "confirm.request", "request": { "id": "env_ab12", "kind": "provider-switched", "title": "Switch from Anthropic to OpenAI?", "body": "...",
    "fromProviderId": "provider:anthropic", "toProviderId": "provider:openai", "needsProviderChoice": false } }
{ "t": "confirm.resolved", "id": "env_ab12", "decision": "confirmed" }
```
