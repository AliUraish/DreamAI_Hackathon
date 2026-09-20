# Chowkidaar

**APIs that maintain their own integrations.**

*Chowkidaar* means watchman. It stands between a codebase and every external API it depends on
(OpenAI, Stripe, Supabase, Clerk, Twilio, ...). One private AI agent per repository watches that
repository's API pipelines, acts when it is safe, asks when it is not, and opens tested pull requests.
It never merges.

Coding assistants fix an integration after someone notices it broke and asks. Chowkidaar starts from
the change itself: nobody has to notice, and nobody has to prompt.

```
        senses                          understands                       acts
┌──────────────────────┐      ┌──────────────────────────┐      ┌────────────────────────┐
│ API contract drift   │      │ the API pipeline graph:  │      │ reads the provider docs│
│ environment changes  │ ───► │ provider → call sites →  │ ───► │ writes the change      │
│ traffic and pressure │      │ what depends on them     │      │ runs YOUR checks       │
└──────────────────────┘      └──────────────────────────┘      │ opens the pull request │
                                                                │ handles review, merge  │
                                                                └────────────────────────┘
```

## What it does

**Connects without seeing your secrets.** Onboarding gives you an API key (shown once; only its hash is
stored). One command inside your project reports the repository and its credential-like environment
variables. Each value is reduced on your machine to a keyed hash; only the name and that fingerprint
are sent.

**Maps only what matters.** It finds every API call site (AST search with ast-grep), builds the code
graph locally (Graphify, tree-sitter, no LLM), and keeps the API pipeline: the provider, the functions
that call it, and everything that depends on those. On a real 888-node repository the pipeline graph is
61 nodes. The graph grows on screen from the API outwards. Click any node or connection and the agent
explains what it does there and what breaks if the API changes.

**Watches with three senses.**

| Sense | How | What happens |
|---|---|---|
| API contract drift | Polls live responses, infers the response shape, hashes and diffs it. Reads `Deprecation`, `Sunset` and `Link rel="successor-version"` headers. Accepts provider release webhooks. | Breaking change → migration run |
| Environment changes | A variable's name says which provider it is; the hash of its value says whether it is the same credential. | Same credential, new name → handled automatically. Different provider (Anthropic → OpenAI) → asks, and waits. A new key of a kind you already use → recommends a better route. |
| Traffic | Calls per second, p95 and load on every function, drawn as moving dots and load rings. Every 5 minutes the agent silently audits the whole pipeline and writes an audit log. | A node past its budget → pressure review |

**Acts, with your checks as the gate.** The model writes whole files; Chowkidaar runs the project's own
tests, typecheck and build in a throwaway clone, then opens a GitHub pull request with the evidence.
If validation fails twice it opens an *investigation* PR that changes no application code.

**Follows through.** Review comments on the PR become another round of work on the same branch
(comments from deploy and CI bots are ignored). After a merge it re-runs the checks on the base branch
before marking the integration healthy. GitHub is the record of what was opened: when a project is
(re)connected, open pull requests on `chowkidaar/*` branches that the database does not know are picked up and
watched again.

**One agent per repository.** Each has its own context and memory; agents run in parallel and share
nothing. The "Agents working" panel shows who is busy and who is waiting for you.

## Principles

- **Numbers come from measurement or simulation, never from the model's opinion.** Performance gains in
  a review are the output of a queueing model (M/M/c) on observed rates, and are labelled *simulated*.
  There is no invented "confidence: 94%".
- **Your checks decide.** A check that already fails before the change is reported, not hidden. If
  nothing can validate a change, no PR is opened.
- **Secrets stay where they are.** Values are never stored, logged, returned by the API or sent to the
  model. Real `.env` files never reach the model.
- **Small blast radius.** The model only sees the files in the affected pipeline, may only write files
  it was given, and tests are read-only context (except test doubles in a provider switch).
- **It never merges, and it never touches your checkout.** All work happens in disposable clones under
  `~/.chowkidaar`.

## Quick start

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), Node 20.19+ (22 recommended), git. Optional: the GitHub CLI
(`gh`), whose login is used as the GitHub token if you do not set one.

```bash
# 1. backend  (http://localhost:8000, API docs at /docs)
cd backend
cp .env.example .env          # set OPENAI_KEY and, optionally, NEON_DB
uv run uvicorn app.main:app --port 8000

# 2. frontend (http://localhost:5173)
cd frontend
npm install
npm run dev
```

Open http://localhost:5173, create a workspace, and copy the connect command it shows. Run it inside
the project you want watched:

```bash
curl -fsSL http://localhost:8000/api/v1/connector.py | CHOWKIDAAR_API_KEY=ck_live_... python3 - --watch
```

The project opens in the UI and its pipeline graph grows in. `--watch` keeps reporting environment
changes. You can also connect a project from the UI by `owner/repo` or a local path.

### Configuration (`backend/.env`)

| Variable | Purpose |
|---|---|
| `OPENAI_KEY` | Writes migrations, addresses PR reviews, words explanations. Without it Chowkidaar still maps, watches and explains from graph facts, and opens investigation PRs instead of code changes. |
| `NEON_DB` | Postgres connection string. Without it a local SQLite file is used. |
| `CHOWKIDAAR_GITHUB_TOKEN` | For pushing branches and opening PRs. Falls back to `gh auth token`. |
| `CHOWKIDAAR_OPEN_PRS` | `false` prepares branches locally and never pushes. |
| `CHOWKIDAAR_DATA_DIR` | Where working copies, graphs, audit logs and keys live. Default `~/.chowkidaar`. |
| `CHOWKIDAAR_OPENAI_MODEL` | Default `gpt-5`. |
| `CHOWKIDAAR_AUDIT_SECONDS` / `_POLL_INTERVAL_SECONDS` / `_PR_WATCH_SECONDS` | Audit (300), contract polling (off; 21600 = 6 h), PR watching (20). |

See `backend/.env.example` for the rest.

## Try it without a real project

A demo API provider and a demo customer app are included.

```bash
# the demo provider (Acme Orders API, v1 with a switch to v2)
uv run --project backend uvicorn main:app --app-dir demo/provider --port 4010
(cd demo/customer-app && npm install)      # once

./demo/run_demo.sh            # the provider ships v2 and announces it
./demo/run_demo.sh poll       # it ships silently; Chowkidaar notices on its own
./demo/run_env_demo.sh        # an env credential changes provider: sense → ask → confirm → migrate
```

The v2 release renames `name → customer_name` and `price → amount`, and moves `/v1/orders → /v2/orders`.
The shape diff sees two renames. Only the migration guide says `amount` is integer cents, not dollars.
A rename-only patch prints `$1999.00` instead of `$19.99`, fails the app's tests against the live API,
and is never shipped.

In the UI, the Monitoring panel has one **Play** button for the simulation: normal load, a prediction of every
call site at peak (queueing model), then the peak itself along the project's real call graph. Below it the agent
shows predicted against measured load, p95, failures and a 0-100 score per node and for the pipeline (arithmetic on
the measurements, never the model's opinion), what holds, what is weak, and a recommended change with simulated
before/after numbers. While traffic flows, call sites past half their budget get
a dashed "add a node here" proposal drawn on the graph. Nothing is changed until you press Apply.

## Traffic from a real service

`GET /api/v1/chowkidaar-traffic.ts` is a small reporter for TypeScript services (Cloudflare Workers,
Node, Bun, Deno). It wraps the functions you name, records how long each call took and whether it
threw, and posts batches to `POST /api/v1/traffic`. It never reads arguments or return values.
Reported traffic replaces simulated traffic; the two are never mixed, and the UI always says which one
it is showing.

## Repository layout

```
backend/     FastAPI service: sensing, graph, agents, pipelines, GitHub, audits   (backend/README.md)
frontend/    React + canvas UI: the pipeline graph, runs, reviews, prompts         (frontend/README.md)
demo/        provider/      mock Acme Orders API (v1 → v2)
             customer-app/  a small TypeScript app that depends on it (plain files;
                            its git repository is created on demand under ~/.chowkidaar)
```

| Backend module | Role |
|---|---|
| `scanner.py`, `graph.py` | API call sites (ast-grep); Graphify code graph, sliced to the pipeline |
| `schema/`, `poller.py` | Response-shape inference, hashing, diff, rename candidates; drift probes |
| `envwatch.py`, `workspace.py` | Environment sensing with fingerprints; workspace and API keys |
| `traffic.py`, `audit.py`, `simrun.py` | Traffic store and simulator (M/M/c); silent audits; one-button simulation |
| `pipeline.py`, `perf.py`, `repair.py` | Migration runs; pressure reviews; the model's patch |
| `prs.py`, `gitops.py` | PR lifecycle (review rounds, merge verification); git and GitHub |
| `agents.py`, `explain.py`, `notify.py` | Per-repo agents with private memory; explanations; notifications |
| `db.py`, `llm.py` | Postgres/SQLite with an in-memory mirror and write-behind; OpenAI first, Anthropic fallback |

## Tests

```bash
cd backend && uv run pytest          # 35 tests; the end-to-end ones need the demo provider on :4010
CHOWKIDAAR_TEST_PG=postgresql://user@localhost:5432/test uv run pytest   # same suite on Postgres
```

The test session strips real credentials before anything runs: tests never reach your database, your
LLM key or your GitHub account.

## Status and limits

Built at a hackathon. What has been exercised for real: connecting a production-style repository
(Cloudflare Workers + Next.js, four providers), the pipeline graph and AI explanations, environment
sensing, the pressure review, and pull requests opened on GitHub with the project's typecheck and
build passing. What has only run against a stubbed GitHub in tests: acting on review comments and
verifying a merge. Traffic in the pressure demo was simulated along the repository's real call graph;
the predicted gains are model output until re-measured on live traffic. The storage layer assumes one
backend process per database. Contract probes are GET-only and exist for a few providers; others are
watched through environment changes and release webhooks.

## Credits

The schema inference, hashing and diff in `backend/app/schema/` are a Python port of
[`@schema-watch/core`](https://github.com/HenryMorganDibie/schema-watch) (Apache-2.0); the
rename-candidate heuristic is inspired by
[api-schema-differentiator](https://github.com/77QAlab/api-schema-differentiator) (MIT). The code graph
is built with [Graphify](https://github.com/Graphify-Labs/graphify) (Apache-2.0) and call sites are
found with [ast-grep](https://github.com/ast-grep/ast-grep) (MIT). See `backend/NOTICE`.
