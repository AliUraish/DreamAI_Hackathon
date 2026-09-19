import { useEffect, useState } from "react";
import { pathToProvider, type View } from "../lib/derive";
import { useStore } from "../lib/store";
import { STAGES, type BreakingChange, type HitRole, type Outcome, type StageId } from "../lib/types";
import { api } from "../data/api";
import { refresh, replayRun } from "../data/source";
import { DiffView } from "./DiffView";
import { Inspector } from "./Inspector";
import { ago, focusProps, Icon, integrationState, NodeRow, Spinner, STATUS_LABEL, useIntegrations, useNodeMap, useStatuses } from "./shared";

const KIND_LABEL: Record<BreakingChange["kind"], string> = {
  endpoint: "endpoint",
  request: "field renamed",
  response: "field renamed",
  semantic: "renamed + unit change",
};

export function StagePanel() {
  const tab = useStore((s) => s.panelTab);
  const setTab = useStore((s) => s.setTab);
  const selectedId = useStore((s) => s.selectedId);
  return (
    <aside className="panel panel-right">
      <div className="tabs" role="tablist">
        <button role="tab" aria-selected={tab === "pipeline"} className={tab === "pipeline" ? "is-on" : ""} onClick={() => setTab("pipeline")}>
          Pipeline
        </button>
        <button role="tab" aria-selected={tab === "inspector"} className={tab === "inspector" ? "is-on" : ""} onClick={() => setTab("inspector")}>
          Inspector{selectedId ? <i className="tab-dot" /> : null}
        </button>
      </div>
      <div className="panel-scroll">{tab === "pipeline" ? <PipelineBody /> : <Inspector />}</div>
    </aside>
  );
}

/** A pressure run walks the same seven stages under names that fit it. */
const PRESSURE_STAGE: Partial<Record<StageId, { label: string; blurb: string }>> = {
  detect: { label: "Detect", blurb: "A node is running past its budget" },
  diff: { label: "Measure", blurb: "Who sends the traffic, and what each call costs" },
  trace: { label: "Trace", blurb: "What shares this call site" },
  docs: { label: "Review", blurb: "Ways to relieve it, each one simulated" },
  patch: { label: "Patch", blurb: "Write the recommended change" },
  verify: { label: "Verify", blurb: "The project's checks, then the simulation again" },
};
const stageMeta = (view: View, i: number) => ({ ...STAGES[i], ...(view.kind === "pressure" ? PRESSURE_STAGE[STAGES[i].id] : undefined) });

/** Where the run stands. Not a control: runs are driven by the agent, not by the viewer. */
function StageStrip({ view }: { view: View }) {
  const ok = !!view.pr || validated(view);
  return (
    <ol className="strip" aria-label="Pipeline stages">
      {STAGES.map((s, i) => {
        const state = i < view.stageIndex ? "done" : i > view.stageIndex ? "todo" : !view.done ? "now" : ok ? "done" : "halt";
        const meta = stageMeta(view, i);
        return <li key={s.id} className={`is-${state}`} title={meta.blurb}><i />{meta.label}</li>;
      })}
    </ol>
  );
}

function PipelineBody() {
  const view = useStore((s) => s.view);
  const mapping = useStore((s) => s.mapping);
  const graphReady = useStore((s) => s.graph.nodes.length > 0);
  const closeRun = useStore((s) => s.closeRun);
  if (!view.started || !view.stage) return mapping.length > 0 && (!graphReady || mapping.at(-1)?.phase !== "done") ? <Mapping /> : <Armed />;
  const meta = stageMeta(view, view.stageIndex);
  return (
    <div className="stage" key={view.stage}>
      <StageStrip view={view} />
      {view.done && <button className="btn" onClick={closeRun}><Icon.prev /> Back to monitoring</button>}
      <header className="stage-head">
        <span className="stage-num mono">
          {String(view.stageIndex + 1).padStart(2, "0")}
          <i>/07</i>
        </span>
        <div>
          <h2>{meta.label}</h2>
          <p>{meta.blurb}</p>
        </div>
      </header>
      {view.done && !view.pr && (validated(view) ? (
        <div className="ended is-ok rise">
          <span className="eyebrow mint">validated · no pull request</span>
          <p>{view.feed.at(-1)?.msg ?? "The branch is ready locally."} Connect a repository with a GitHub remote to get the PR.</p>
        </div>
      ) : (
        <div className="ended rise">
          <span className="eyebrow amber">run ended without a pull request</span>
          <p>{view.feed.at(-1)?.msg ?? "The backend closed the run before opening a PR."}</p>
        </div>
      ))}
      <StageBody stage={view.stage} view={view} />
    </div>
  );
}

// --- the agent building its context for a project ------------------------------------
function Mapping() {
  const steps = useStore((s) => s.mapping);
  const running = steps.at(-1)?.phase !== "done";
  return (
    <div className="stage">
      <Head title="Building context" blurb="The agent is learning this project's API pipelines" />
      <ol className="mapping">
        {steps.map((m, i) => {
          const last = i === steps.length - 1;
          return <li key={i} className="rise">{last && running ? <Spinner /> : <span className="mint"><Icon.check /></span>}<span>{m.msg}</span></li>;
        })}
      </ol>
      <p className="note">The graph is drawn from the API outwards as the context fills in: the provider, the functions that call it, then whatever depends on those.</p>
    </div>
  );
}

/** Every check that ran against the patched code passed. */
function validated(view: View): boolean {
  const after = view.checks.filter((c) => c.phase === "patched");
  return after.length > 0 && after.every((c) => c.status === "passed");
}

function StageBody({ stage, view }: { stage: StageId; view: View }) {
  switch (stage) {
    case "detect": return <Detect view={view} />;
    case "diff": return <Diff view={view} />;
    case "trace": return <Trace view={view} />;
    case "docs": return view.kind === "pressure" ? <ReviewBody view={view} /> : <Docs view={view} />;
    case "patch": return <Patch view={view} />;
    case "verify": return <Verify view={view} />;
    case "pr": return <Pr view={view} />;
  }
}

// --- 00 · armed -----------------------------------------------------------------
/** Runs a backend action with a busy flag and the error, if any, shown in place. */
function useAction() {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const run = (name: string, fn: () => Promise<unknown>) => {
    setBusy(name);
    setError(null);
    fn().then(() => refresh()).catch((e: Error) => setError(e.message)).finally(() => setBusy(null));
  };
  return { busy, error, run };
}

function Armed() {
  const graph = useStore((s) => s.graph);
  const connection = useStore((s) => s.connection);
  const demo = useStore((s) => s.demo);
  const lastRun = useStore((s) => s.lastRun);
  const integrations = useIntegrations();
  const { busy, error, run } = useAction();
  const [polled, setPolled] = useState<string | null>(null);
  const providers = graph.nodes.filter((n) => n.kind === "provider");
  const hasDemoApi = integrations.has("provider:acme-orders") && demo.available;

  if (connection === "offline")
    return (
      <div className="stage">
        <Head title={connection === "offline" ? "Backend offline" : "Connecting"} blurb="http://localhost:8000" />
        <p className="note">
          {connection === "offline" ? <>The Chowkidaar backend is not answering. Start it with <code className="mono">uv run uvicorn app.main:app --port 8000</code> in <code className="mono">backend/</code>; this page reconnects by itself.</> : <><Spinner /> Reaching the backend</>}
        </p>
      </div>
    );

  return (
    <div className="stage">
      <Head title="Monitoring" blurb="Watching every provider this repo calls" />
      <ul className="watch">
        {providers.map((p) => {
          const i = integrations.get(p.id);
          const state = i ? integrationState(i) : null;
          return (
            <li key={p.id} {...focusProps([p.id, ...neighborsOf(graph.links, p.id)])}>
              <span className="dot" style={{ "--c": `var(--${state?.tone === "muted" ? "faint" : state?.tone ?? "mint"})` } as React.CSSProperties} />
              <span>
                {p.label} <span className="mono muted">{p.version}</span>
                <small className="watch-sub mono">{i?.monitored ? `polled ${ago(i.last_checked_at)}` : "no probe configured · watched through env and releases"}</small>
              </span>
              <span className={`state ${state?.tone ?? "mint"}`}>{state?.label}</span>
            </li>
          );
        })}
      </ul>
      <p className="note">
        Signals: provider release webhooks, contract polling, environment changes. Nothing to do until one fires.
      </p>
      <button
        className="btn"
        disabled={!!busy}
        onClick={() => run("check", async () => {
          const results = await api.checkAll();
          const drift = results.filter((r) => r.changes.length).length;
          setPolled(drift ? `${drift} integration(s) drifted` : `${results.length} polled · no drift`);
        })}
      >
        {busy === "check" ? <Spinner /> : <Icon.restart />} Poll contracts now
        {polled && busy !== "check" && <span className="muted small">{polled}</span>}
      </button>

      <Simulation />
      <TrafficControls />
      {hasDemoApi && (
        <section className="group demo">
          <h3>Demo provider<i>{demo.version}</i></h3>
          {demo.version === "v1" ? (
            <>
              <button className="cta" disabled={!!busy} onClick={() => run("release", () => api.demoRelease(true))}>
                <span>Ship Orders API v2<small>The provider publishes a breaking release and announces it</small></span>
                {busy === "release" ? <Spinner /> : <Icon.arrow />}
              </button>
              <button className="btn" disabled={!!busy} onClick={() => run("silent", async () => { await api.demoRelease(false); await api.checkAll(); })}>
                {busy === "silent" ? <Spinner /> : <Icon.follow />} Ship it silently, then poll
                <span className="muted small">nobody tells Chowkidaar</span>
              </button>
            </>
          ) : (
            <button className="btn" disabled={!!busy} onClick={() => run("reset", () => api.demoReset())}>
              {busy === "reset" ? <Spinner /> : <Icon.restart />} Reset the demo to v1
              <span className="muted small">reconnects the repository</span>
            </button>
          )}
        </section>
      )}

      {lastRun && (
        <section className="group">
          <h3>Latest work by this project's agent</h3>
          <button className="btn" disabled={!!busy} onClick={() => run("replay", () => replayRun(lastRun.id))}>
            {busy === "replay" ? <Spinner /> : <Icon.play />}
            <span className="btn-main">{lastRun.title}<small className="mono muted">{lastRun.trigger} · {lastRun.status.replace("_", " ")}{lastRun.review_rounds ? ` · ${lastRun.review_rounds} review round(s)` : ""} · {ago(lastRun.created_at)}</small></span>
          </button>
          {lastRun.pr_url && (
            <a className="btn" href={lastRun.pr_url} target="_blank" rel="noreferrer">
              <Icon.external /> Pull request #{lastRun.pr_number}
              <span className={`state ${lastRun.pr_state === "merged" ? "mint" : lastRun.pr_state === "closed" ? "coral" : "violet"}`}>{lastRun.pr_state ?? "open"}</span>
            </a>
          )}
        </section>
      )}
      {error && <p className="error">{error}</p>}
    </div>
  );
}

/** One button: the agent simulates normal then peak load on this project's real call graph, measures every node,
 *  says what is healthy and what is weak, and recommends a change. It opens nothing until the user applies it. */
function Simulation() {
  const projectId = useStore((s) => s.projectId)!;
  const sim = useStore((s) => s.sim);
  const flyTo = useStore((s) => s.flyTo);
  const { busy, error, run } = useAction();
  const report = sim.report;
  const rec = report?.recommendation;
  const worst = rec ? Math.max(rec.before!.p95_ms, rec.after!.p95_ms) : 1;
  return (
    <section className="group demo">
      <h3>Simulation{report && <i>{report.writtenBy === "simulation" ? "model" : `model + ${report.writtenBy}`}</i>}</h3>
      <button className="cta cta-mint" disabled={sim.running || !!busy} onClick={() => run("sim", () => api.runSimulation(projectId))}>
        <span>{report ? "Run the simulation again" : "Run a simulation"}<small>Normal load, then peak. The agent measures every node and tells you what holds and what does not.</small></span>
        {sim.running || busy === "sim" ? <Spinner /> : <Icon.play />}
      </button>
      {sim.running && (
        <ol className="mapping">
          {sim.steps.map((st, i) => <li key={i} className="rise">{i === sim.steps.length - 1 ? <Spinner /> : <span className="mint"><Icon.check /></span>}<span>{st.msg}</span></li>)}
        </ol>
      )}
      {report && !sim.running && (
        <div className="simreport rise">
          <p className="explain-text">{report.headline}</p>
          <div className="verdicts">
            <div>
              <span className="eyebrow mint">holds up</span>
              <ul>{report.goodText.map((t, i) => <li key={i} {...focusProps(report.good[i]?.nodeIds ?? [])}>{t}</li>)}</ul>
            </div>
            <div>
              <span className="eyebrow coral">weak</span>
              <ul>{report.badText.length ? report.badText.map((t, i) => <li key={i} {...focusProps(report.bad[i]?.nodeIds ?? [])} onClick={() => report.bad[i] && flyTo(report.bad[i].nodeIds, 1.7)}>{t}</li>) : <li>Nothing at this load.</li>}</ul>
            </div>
          </div>
          <table className="metrics mono">
            <thead><tr><th>node at peak</th><th>calls/s</th><th>p95</th><th>load</th></tr></thead>
            <tbody>
              {report.metrics.slice(0, 6).map((m) => (
                <tr key={m.node} {...focusProps([m.node])}>
                  <td>{m.label}</td><td>{m.rps.toFixed(1)}</td><td>{seconds(m.p95_ms)}</td>
                  <td className={m.load >= 0.85 ? "coral" : m.load >= 0.6 ? "amber" : "mint"}>{m.load.toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {rec && (
            <div className="card option is-best" {...focusProps([rec.nodeId, ...rec.new_nodes.map((n) => n.from)])}>
              <span className="eyebrow mint">recommendation</span>
              <b>{rec.title}</b>
              <span className="muted small">{report.advice}</span>
              <Bar label="p95 now" value={rec.before!.p95_ms} max={worst} tone="coral" text={rec.before!.saturated ? "times out" : seconds(rec.before!.p95_ms)} />
              <Bar label="p95 after" value={rec.after!.p95_ms} max={worst} tone="mint" text={seconds(rec.after!.p95_ms)} />
              <span className="chips">
                <span className="chip mono">p95 −{rec.gain!.p95_pct}%</span>
                <span className="chip mono">capacity +{rec.gain!.capacity_pct}%</span>
                <span className="chip mono">simulated</span>
              </span>
              <button className="btn btn-go" disabled={!!busy} onClick={() => run("apply", () => api.startReview(projectId, rec.nodeId, rec.prefer))}>
                {busy === "apply" ? <Spinner /> : <Icon.check />} Apply: write it, run the checks, open a PR
              </button>
            </div>
          )}
        </div>
      )}
      {error && <p className="error">{error}</p>}
    </section>
  );
}

/** Traffic for this project: reported by the project itself, or simulated along its real call graph. */
function TrafficControls() {
  const projectId = useStore((s) => s.projectId)!;
  const traffic = useStore((s) => s.traffic);
  const recommendations = useStore((s) => s.recommendations);
  const nodes = useNodeMap(useStore((s) => s.graph.nodes));
  const { busy, error, run } = useAction();
  const hot = traffic ? Object.entries(traffic.nodes).filter(([id]) => !id.startsWith("provider:")).sort((a, b) => b[1].load - a[1].load)[0] : undefined;
  return (
    <section className="group demo">
      <h3>Traffic<i>{traffic ? traffic.source : "none"}</i></h3>
      {traffic && hot ? (
        <p className="note">
          Busiest: <b className="mono">{nodes.get(hot[0])?.label ?? hot[0]}</b> at load <b className={hot[1].load >= 0.85 ? "coral" : hot[1].load >= 0.6 ? "amber" : "mint"}>{hot[1].load.toFixed(2)}</b>,
          {" "}{hot[1].rps.toFixed(1)} calls/s, p95 {seconds(hot[1].p95_ms)}.
          {traffic.source === "simulated" && " Simulated along this project's real call graph."}
        </p>
      ) : <p className="note">No traffic reported. Add the reporter to the project, or simulate it along the project's real call graph.</p>}
      {(traffic?.suggestions ?? []).map((sg) => (
        <div key={sg.nodeId} className="card option rise" {...focusProps([sg.nodeId, ...sg.new_nodes.flatMap((n) => [n.id, n.from])])}>
          <span className="eyebrow amber">traffic suggests · add {sg.new_nodes.length === 1 ? "a node" : `${sg.new_nodes.length} nodes`}</span>
          <b>{sg.title}</b>
          <span className="muted small">{sg.label} is at load {sg.before.load.toFixed(2)} of {sg.before.budget} slots. Drawn dashed on the graph.</span>
          <span className="chips">
            <span className="chip mono">p95 {seconds(sg.before.p95_ms)} → {seconds(sg.after.p95_ms)}</span>
            <span className="chip mono">capacity +{sg.gain.capacity_pct}%</span>
            <span className="chip mono">simulated</span>
          </span>
          <button className="btn btn-go" disabled={!!busy} onClick={() => run(`sg:${sg.nodeId}`, () => api.startReview(projectId, sg.nodeId, sg.prefer))}>
            {busy === `sg:${sg.nodeId}` ? <Spinner /> : <Icon.check />} Apply: write it, run the checks, open a PR
          </button>
        </div>
      ))}
      {recommendations.map((r) => (
        <div key={r.id} className="card option is-best rise" {...focusProps([r.nodeId])}>
          <span className="eyebrow mint">better route available</span>
          <b>{r.title}</b>
          <span className="muted small">{r.summary}</span>
          {r.before && r.after && <span className="chips"><span className="chip mono">p95 {seconds(r.before.p95_ms)} → {seconds(r.after.p95_ms)}</span><span className="chip mono">simulated</span></span>}
          <div className="confirm-actions">
            <button className="btn" onClick={() => useStore.getState().dropRecommendation(r.id)}>Not now</button>
            <button className="btn btn-go" disabled={!!busy} onClick={() => run("apply", async () => { await api.startReview(projectId, r.nodeId, r.prefer); useStore.getState().dropRecommendation(r.id); })}>
              {busy === "apply" ? <Spinner /> : <Icon.check />} Apply and open a PR
            </button>
          </div>
        </div>
      ))}
      {!traffic ? (
        <button className="btn" disabled={!!busy} onClick={() => run("steady", () => api.simulate(projectId, "steady"))}>
          {busy === "steady" ? <Spinner /> : <Icon.play />} Simulate traffic
        </button>
      ) : traffic.source === "simulated" && (
        <>
          {traffic.simulation?.profile !== "pressure" && (
            <button className="cta" disabled={!!busy} onClick={() => run("pressure", () => api.simulate(projectId, "pressure"))}>
              <span>Put the busiest call site under pressure<small>Traffic rises until it runs at its budget and calls start to queue</small></span>
              {busy === "pressure" ? <Spinner /> : <Icon.arrow />}
            </button>
          )}
          <button className="btn" disabled={!!busy} onClick={() => run("audit", () => api.auditNow(projectId))}>
            {busy === "audit" ? <Spinner /> : <Icon.follow />} Run the agent's audit now
            <span className="muted small">otherwise every 5 min, silently</span>
          </button>
          <button className="btn" disabled={!!busy} onClick={() => run("off", () => api.simulate(projectId, "off"))}><Icon.x /> Stop the simulation</button>
        </>
      )}
      {error && <p className="error">{error}</p>}
    </section>
  );
}

function Head({ title, blurb }: { title: string; blurb: string }) {
  return (
    <header className="stage-head">
      <span className="stage-num mono">00<i>/07</i></span>
      <div>
        <h2>{title}</h2>
        <p>{blurb}</p>
      </div>
    </header>
  );
}

function neighborsOf(links: { source: string; target: string }[], id: string): string[] {
  return links.flatMap((l) => (l.source === id ? [l.target] : l.target === id ? [l.source] : []));
}

// --- 01 · detect ----------------------------------------------------------------
function Detect({ view }: { view: View }) {
  const nodes = useNodeMap(useStore((s) => s.graph.nodes));
  const r = view.release;
  if (!r) return <p className="note"><Spinner /> Polling provider contracts</p>;
  const trigger = view.kind === "pressure"
    ? { eyebrow: "node under pressure", text: "The agent's audit of this project's traffic. Nobody filed a ticket." }
    : r.source.startsWith("environment")
    ? { eyebrow: "provider change confirmed", text: "A credential in the environment changed provider, and you confirmed it." }
    : r.source.startsWith("observed")
      ? { eyebrow: "drift observed", text: "Chowkidaar's own polling of the live API. Nobody announced anything." }
      : { eyebrow: "release detected", text: "The provider's release. No failing request, no bug report, no prompt." };
  return (
    <>
      <div className="release" {...focusProps([r.providerId])}>
        <span className="eyebrow coral">{trigger.eyebrow}</span>
        <div className="release-name">{nodes.get(r.providerId)?.label ?? r.providerId}</div>
        <div className="release-ver mono">
          <span>{r.from}</span>
          <Icon.arrow />
          <span className="coral">{r.to}</span>
        </div>
      </div>
      <dl className="kv">
        <dt>signal</dt>
        <dd className="mono">{r.source}</dd>
        <dt>trigger</dt>
        <dd>{trigger.text}</dd>
      </dl>
    </>
  );
}

// --- 02 · diff ------------------------------------------------------------------
function Diff({ view }: { view: View }) {
  const flyTo = useStore((s) => s.flyTo);
  return (
    <>
      <div className="cards">
        {view.changes.map((c) => (
          <button key={c.id} className="card change rise" {...focusProps(c.nodeIds)} onClick={() => flyTo(c.nodeIds, 1.6)}>
            <span className={`eyebrow ${c.kind === "semantic" ? "amber" : "coral"}`}>{c.label ?? KIND_LABEL[c.kind]}</span>
            <span className="change-line mono">
              <s>{c.before}</s>
              <Icon.arrow />
              <b>{c.after}</b>
            </span>
            <span className="muted small">{c.where}</span>
            {c.note && <span className="change-note">{c.note}</span>}
            <span className="touch mono">{c.nodeIds.length} nodes touch this</span>
          </button>
        ))}
        {view.changes.length === 0 && <p className="note"><Spinner /> Diffing contracts</p>}
      </div>
      <p className="hint">Hover a change to see where the repo touches it.</p>
    </>
  );
}

// --- 03 · trace -----------------------------------------------------------------
const ROLE_TITLE: Record<HitRole, string> = {
  change: "Files to change",
  symbol: "On the path",
  dependent: "Dependents, re-verified",
  test: "Tests that cover them",
};

function Trace({ view }: { view: View }) {
  const graph = useStore((s) => s.graph);
  const nodes = useNodeMap(graph.nodes);
  const statuses = useStatuses();
  const hits = [...view.hits.values()];
  const toChange = hits.filter((h) => h.role === "change").length;
  return (
    <>
      <div className="stats">
        <div><b className="mono">{graph.nodes.length}</b><span>nodes in graph</span></div>
        <div><b className="mono">{hits.length}</b><span>reached</span></div>
        <div><b className="mono coral">{toChange}</b><span>files to change</span></div>
      </div>
      <p className="note">
        Only the files in this pipeline go to the model{graph.repoNodesTotal ? <>: <b>{graph.nodes.length}</b> of the repo's {graph.repoNodesTotal} graph nodes</> : null}. The rest of the repo is never read.
      </p>
      {(["change", "symbol", "dependent", "test"] as HitRole[]).map((role) => {
        const group = hits.filter((h) => h.role === role);
        if (!group.length) return null;
        return (
          <section key={role} className="group">
            <h3>{ROLE_TITLE[role]}<i>{group.length}</i></h3>
            {group.map((h) => {
              const n = nodes.get(h.nodeId);
              return n ? <NodeRow key={h.nodeId} node={n} status={statuses.get(h.nodeId)} meta={`hop ${h.hop}`} /> : null;
            })}
          </section>
        );
      })}
      <p className="hint">Hover a row to light its path back to the API.</p>
    </>
  );
}

// --- 04 · docs ------------------------------------------------------------------
function Docs({ view }: { view: View }) {
  if (!view.docs) return <p className="note"><Spinner /> Fetching the migration guide</p>;
  return (
    <>
      <a className="doc-title" href={view.docs.url} target="_blank" rel="noreferrer">
        <span>{view.docs.title}</span>
        <Icon.external />
      </a>
      <div className="cards">
        {view.excerpts.map((ex) => (
          <div key={ex.id} className="card excerpt rise" {...focusProps(ex.nodeIds)}>
            <span className="eyebrow blue">{ex.section}</span>
            <p>{ex.text}</p>
            <span className="chips">
              {ex.changeIds.map((id) => {
                const c = view.changes.find((x) => x.id === id);
                return c ? <span key={id} className="chip mono">{c.before} → {c.after}</span> : null;
              })}
              <span className="touch mono">applies to {ex.nodeIds.length} files</span>
            </span>
          </div>
        ))}
      </div>
      {view.changes.some((c) => c.kind === "semantic") && (
        <p className="hint">A change of meaning is only stated in the guide. A contract diff alone would have missed it.</p>
      )}
    </>
  );
}

// --- 04 · review (pressure runs) ---------------------------------------------------------------
const seconds = (ms: number) => (ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`);

function Bar({ label, value, max, tone, text }: { label: string; value: number; max: number; tone: string; text: string }) {
  return (
    <div className="bar">
      <span className="mono muted">{label}</span>
      <i><b className={tone} style={{ width: `${Math.max(3, Math.min(100, (value / max) * 100))}%` }} /></i>
      <span className={`mono ${tone}`}>{text}</span>
    </div>
  );
}

function ReviewBody({ view }: { view: View }) {
  const review = view.review;
  const flyTo = useStore((s) => s.flyTo);
  if (!review) return <p className="note"><Spinner /> Simulating the options on the observed traffic</p>;
  const worst = Math.max(review.before.p95_ms, ...review.options.map((o) => o.after.p95_ms));
  const out = (o: Outcome) => (o.saturated ? "times out" : seconds(o.p95_ms));
  return (
    <>
      <div className="ended rise">
        <span className="eyebrow coral">{review.label} · load {review.before.load.toFixed(2)} of {review.before.budget} slots</span>
        <p>{review.diagnosis}</p>
      </div>
      <div className="cards">
        {review.options.map((o) => {
          const best = o.id === review.recommended;
          return (
            <button key={o.id} className={`card option rise${best ? " is-best" : ""}`} {...focusProps([review.nodeId, ...o.new_nodes.flatMap((n) => [n.id, n.from])])}
              onClick={() => flyTo([review.nodeId, ...o.new_nodes.flatMap((n) => [n.id, n.from])], 1.7)}>
              <span className={`eyebrow ${best ? "mint" : "muted"}`}>{best ? "recommended · " : ""}{o.kind === "split" ? "add nodes" : o.kind === "route" ? "reroute traffic" : "keep one node"}</span>
              <b>{o.title}</b>
              <span className="muted small">{o.summary}</span>
              <Bar label="p95 now" value={review.before.p95_ms} max={worst} tone="coral" text={out(review.before)} />
              <Bar label="p95 after" value={o.after.p95_ms} max={worst} tone={best ? "mint" : "amber"} text={out(o.after)} />
              <span className="chips">
                <span className="chip mono">p95 −{o.gain.p95_pct}%</span>
                <span className="chip mono">capacity {o.gain.capacity_pct >= 0 ? "+" : ""}{o.gain.capacity_pct}%</span>
                <span className="chip mono">load → {o.after.load.toFixed(2)}</span>
              </span>
            </button>
          );
        })}
      </div>
      <section className="group">
        <h3>Why this one</h3>
        <p className="explain-text">{review.why}</p>
        {review.risks.length > 0 && <ul className="risks">{review.risks.map((r) => <li key={r}>{r}</li>)}</ul>}
      </section>
      <p className="hint">
        Numbers are a queueing simulation (M/M/c) on the {review.source} traffic, not a measurement. The choice was made by {review.decidedBy === "simulation" ? "the simulation alone" : review.decidedBy}.
      </p>
    </>
  );
}

// --- 05 · patch -----------------------------------------------------------------
function Patch({ view }: { view: View }) {
  const nodes = useNodeMap(useStore((s) => s.graph.nodes));
  const files = [...new Set([...[...view.hits.values()].filter((h) => h.role === "change").map((h) => h.nodeId), ...view.patches.keys()])];
  const latest = [...view.patches.keys()].at(-1);
  const [picked, setPicked] = useState<string | null>(null);
  // Follow the file being written unless the viewer chose one.
  useEffect(() => setPicked(null), [view.patches.size === 0]);
  const shown = picked ?? latest;
  const state = shown ? view.patches.get(shown) : undefined;
  return (
    <>
      <div className="files">
        {files.map((id) => {
          const p = view.patches.get(id);
          return (
            <button
              key={id}
              className={`file${shown === id ? " is-on" : ""}`}
              disabled={!p}
              {...focusProps(pathToProvider(view, id))}
              onClick={() => setPicked(id)}
            >
              {p?.status === "done" ? (
                p.patch?.hunks.length ? <span className="mint"><Icon.check /></span> : <span className="amber"><Icon.x /></span>
              ) : p ? <Spinner /> : <span className="queued" />}
              <span className="mono">{nodes.get(id)?.label ?? id}</span>
              {p?.patch && <span className="diff-stat mono"><b className="add">+{p.patch.additions}</b> <b className="del">−{p.patch.deletions}</b></span>}
            </button>
          );
        })}
      </div>
      {state?.patch && state.patch.hunks.length === 0 ? (
        <div className="writing mono">no changes were generated for {state.patch.path}</div>
      ) : state?.patch ? (
        <DiffView patch={state.patch} />
      ) : shown ? (
        <div className="writing mono"><Spinner /> rewriting {shown}</div>
      ) : null}
    </>
  );
}

// --- 06 · verify ----------------------------------------------------------------
function Verify({ view }: { view: View }) {
  const tests = view.checks.filter((c) => c.group === "tests");
  const ids = [...new Set(tests.map((c) => c.id))];
  const other = view.checks.filter((c) => c.group !== "tests");
  const target = view.release?.to ?? "new API";
  const count = (phase: "baseline" | "patched", status: string) => tests.filter((c) => c.phase === phase && c.status === status).length;
  const cell = (id: string, phase: "baseline" | "patched") => {
    const c = tests.find((x) => x.id === id && x.phase === phase);
    if (!c) return <span className="cell" />;
    return (
      <span className={`cell is-${c.status}`} title={c.detail}>
        {c.status === "running" ? <Spinner /> : c.status === "passed" ? <Icon.check /> : <Icon.x />}
      </span>
    );
  };
  return (
    <>
      <div className="verify-head mono">
        <span />
        <span className={count("baseline", "failed") ? "coral" : "muted"}>{target}, unpatched<br /><b>{count("baseline", "failed")} failing</b></span>
        <span className={count("patched", "passed") ? "mint" : "muted"}>{target}, patched<br /><b>{count("patched", "passed")}/{ids.length || "–"} passing</b></span>
      </div>
      <div className="verify">
        {ids.map((id) => {
          const any = tests.find((c) => c.id === id)!;
          const failing = tests.find((c) => c.id === id && c.phase === "baseline" && c.status === "failed");
          return (
            <div key={id} className="verify-row" {...focusProps(any.nodeId ? [any.nodeId] : [])}>
              <span className="verify-name">
                {any.name}
                {failing?.detail && <small className="mono">{failing.detail}</small>}
              </span>
              {cell(id, "baseline")}
              {cell(id, "patched")}
            </div>
          );
        })}
      </div>
      {other.length > 0 && (
        <div className="terminal mono" aria-label="Checks as they ran">
          {other.map((c) => (
            <div key={`${c.id}:${c.phase}`} className={`term-block is-${c.status}`}>
              <div className="term-cmd"><span className="muted">{c.phase === "baseline" ? "base $" : "patched $"}</span> {c.cmd ?? c.name}{c.status === "running" && <i className="cursor" />}</div>
              {c.status !== "running" && (c.output ?? []).slice(-4).map((line, i) => <div key={i} className="term-out">{line}</div>)}
              {c.status !== "running" && (
                <div className={c.status === "passed" ? "mint" : "coral"}>
                  {c.status === "passed" ? "✓" : "✗"} {c.detail ?? c.status}{c.durationMs ? ` · ${(c.durationMs / 1000).toFixed(1)} s` : ""}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </>
  );
}

// --- 07 · pr --------------------------------------------------------------------
function Pr({ view }: { view: View }) {
  const statuses = useStatuses();
  const pr = view.pr;
  if (!pr) return <p className="note"><Spinner /> Pushing branch and opening the pull request</p>;
  const tests = view.checks.filter((c) => c.group === "tests" && c.phase === "patched");
  const passed = tests.filter((c) => c.status === "passed").length;
  const others = view.checks.filter((c) => c.group !== "tests" && c.status === "passed");
  return (
    <>
      <div className="pr rise">
        <span className="eyebrow violet">pull request opened</span>
        <h3>{pr.title} <span className="muted">#{pr.number}</span></h3>
        <div className="pr-branch mono"><Icon.branch /> {pr.branch} <Icon.arrow /> {pr.base}</div>
        <ul className="pr-facts">
          <li><b className="mono">{pr.files}</b> files · <b className="add mono">+{pr.additions}</b> <b className="del mono">−{pr.deletions}</b></li>
          <li className="mint"><Icon.check /> {passed}/{tests.length} tests passed</li>
          {others.map((c) => <li key={c.id} className="mint"><Icon.check /> <span className="mono">{c.name}</span></li>)}
        </ul>
        <a className="cta cta-violet" href={pr.url} target="_blank" rel="noreferrer">
          <span>Open on GitHub<small>Review and merge. Chowkidaar never merges for you.</small></span>
          <Icon.external />
        </a>
      </div>
      {view.merged && (
        <div className={`ended rise ${view.merged.verified ? "is-ok" : ""}`}>
          <span className={`eyebrow ${view.merged.verified ? "mint" : "amber"}`}>{view.merged.verified ? "merged and verified" : "merged · checks failing"}</span>
          <p>The agent ran the project's checks on <code className="mono">{view.merged.base}</code> after the merge: {view.merged.checks}.</p>
        </div>
      )}
      {(view.comments.length > 0 || view.updates.length > 0) && (
        <section className="group">
          <h3>Review<i>{view.comments.length}</i></h3>
          {view.comments.map((c) => (
            <div key={c.id} className="card review rise">
              <span className="eyebrow blue">{c.author}{c.path ? ` · ${c.path}${c.line ? `:${c.line}` : ""}` : ""}</span>
              <p>{c.body}</p>
            </div>
          ))}
          {view.updates.map((u) => (
            <div key={u.commit} className="card review is-fix rise">
              <span className="eyebrow mint">agent · {u.commit}</span>
              <p>{u.summary}</p>
              <span className="muted small mono">{u.checks} · {u.files.join(", ")}</span>
            </div>
          ))}
        </section>
      )}
      {!view.merged && <p className="hint">The agent keeps watching this pull request: review comments become another round of work on the same branch, and a merge is verified on the base branch.</p>}
      <section className="group">
        <h3>Changed files<i>{view.patches.size}</i></h3>
        <PatchedFiles view={view} statuses={statuses} />
      </section>
    </>
  );
}

function PatchedFiles({ view, statuses }: { view: View; statuses: ReturnType<typeof useStatuses> }) {
  const nodes = useNodeMap(useStore((s) => s.graph.nodes));
  return (
    <>
      {[...view.patches.keys()].map((id) => {
        const n = nodes.get(id);
        const s = statuses.get(id);
        return n ? <NodeRow key={id} node={n} status={s} meta={s ? STATUS_LABEL[s] : undefined} /> : null;
      })}
    </>
  );
}
