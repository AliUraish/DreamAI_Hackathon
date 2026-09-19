import { useEffect, useMemo, useState } from "react";
import { api, type Explanation } from "../data/api";
import { useStore } from "../lib/store";
import type { GraphData, GraphNode } from "../lib/types";
import { communityColor, shortLabel, STATUS_COLOR } from "../graph/render";
import { DiffView } from "./DiffView";
import { Icon, NodeRow, Spinner, STATUS_LABEL, useNodeMap, useStatuses } from "./shared";

/** Files that reach a provider within two hops: the repo's integration map for that API. */
export function integrationFiles(graph: GraphData, providerId: string): GraphNode[] {
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  const callers = graph.links.filter((l) => l.target === providerId).map((l) => l.source);
  const files = new Set<string>();
  for (const c of callers) {
    const node = byId.get(c);
    if (node?.kind === "file") files.add(c);
    for (const l of graph.links) {
      if (l.target !== c) continue;
      const src = byId.get(l.source);
      if (src?.kind === "file") files.add(src.id);
    }
  }
  return [...files].map((id) => byId.get(id)!);
}

/** Asked of the project's agent: what is this doing in the pipeline?
 *  The facts from the code graph show at once; the model's wording replaces them when it arrives. */
function Explain({ target }: { target: { nodeId: string } | { source: string; target: string } }) {
  const projectId = useStore((s) => s.projectId);
  const key = "nodeId" in target ? target.nodeId : `${target.source}->${target.target}`;
  const [state, setState] = useState<{ key: string; data?: Explanation; writing?: boolean; error?: string }>({ key });
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!projectId) return;
    let live = true;
    setState({ key });
    api.explain(projectId, target, "facts")
      .then((quick) => {
        if (!live) return;
        const needsAi = quick.source === "graph" && quick.ai_available;
        setState({ key, data: quick, writing: needsAi });
        if (!needsAi) return;
        // The backend may be restarting or the model slow: try twice before giving up, and keep the facts on screen meanwhile.
        const ask = (left: number): Promise<void> => api.explain(projectId, target, "ai")
          .then((full) => { if (live) setState({ key, data: full }); })
          .catch((e: Error) => (left > 0 ? new Promise<void>((r) => setTimeout(r, 2500)).then(() => ask(left - 1)) : Promise.reject(e)));
        return ask(1).catch((e: Error) => { if (live) setState({ key, data: quick, error: e.message }); });
      })
      .catch((e: Error) => live && setState({ key, error: e.message }));
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, key, attempt]);

  const { data, writing, error } = state;
  return (
    <section className="group explain">
      <h3>What this is doing here</h3>
      {!data && !error && <p className="note"><Spinner /> Reading the code graph</p>}
      {data && <p className="explain-text rise" key={data.source}>{data.text}</p>}
      {data && (
        <span className="muted small mono">
          {writing ? <><Spinner /> from the code graph · the AI is writing a fuller explanation</>
            : data.source === "graph" ? (data.ai_available ? "from the code graph" : "from the code graph · set OPENAI_KEY for AI explanations")
            : `written by ${data.source} from the code graph`}
        </span>
      )}
      {error && (
        <p className="error">
          {data ? "The AI explanation could not be fetched" : "This could not be explained"} ({error}).{" "}
          <button className="link" onClick={() => setAttempt((n) => n + 1)}>Try again</button>
        </p>
      )}
    </section>
  );
}

function NodeTrafficStats({ nodeId }: { nodeId: string }) {
  const t = useStore((s) => s.traffic?.nodes[nodeId]);
  const source = useStore((s) => s.traffic?.source);
  if (!t) return null;
  const tone = t.load >= 0.85 ? "coral" : t.load >= 0.6 ? "amber" : "mint";
  return (
    <section className="group">
      <h3>Traffic<i>{source}</i></h3>
      <div className="stats">
        <div><b className="mono">{t.rps.toFixed(1)}</b><span>calls / s</span></div>
        <div><b className="mono">{t.p95_ms >= 1000 ? `${(t.p95_ms / 1000).toFixed(1)}s` : `${Math.round(t.p95_ms)}ms`}</b><span>p95</span></div>
        <div><b className={`mono ${tone}`}>{t.load.toFixed(2)}</b><span>load of {t.budget} slots</span></div>
      </div>
    </section>
  );
}

function LinkInspector({ link }: { link: { source: string; target: string } }) {
  const graph = useStore((s) => s.graph);
  const nodes = useNodeMap(graph.nodes);
  const statuses = useStatuses();
  const a = nodes.get(link.source), b = nodes.get(link.target);
  const relation = graph.links.find((l) => (l.source === link.source && l.target === link.target) || (l.source === link.target && l.target === link.source))?.relation;
  if (!a || !b) return null;
  return (
    <div className="inspector" key={`${link.source}->${link.target}`}>
      <header className="insp-head">
        <span className="dot lg" style={{ "--c": "#fff" } as React.CSSProperties} />
        <div>
          <h2 className="mono">{relation?.replace("_", " ") ?? "connection"}</h2>
          <p>connection between two nodes of the pipeline</p>
        </div>
      </header>
      <section className="group">
        <NodeRow node={a} status={statuses.get(a.id)} meta="from" trail={false} />
        <NodeRow node={b} status={statuses.get(b.id)} meta="to" trail={false} />
      </section>
      <Explain target={link} />
    </div>
  );
}

export function Inspector() {
  const selectedLink = useStore((s) => s.selectedLink);
  if (selectedLink) return <LinkInspector link={selectedLink} />;
  return <NodeInspector />;
}

function NodeInspector() {
  const graph = useStore((s) => s.graph);
  const view = useStore((s) => s.view);
  const selectedId = useStore((s) => s.selectedId);
  const flyTo = useStore((s) => s.flyTo);
  const nodes = useNodeMap(graph.nodes);
  const statuses = useStatuses();

  const node = selectedId ? nodes.get(selectedId) : undefined;
  const edges = useMemo(() => {
    if (!selectedId) return { out: [], in: [] };
    return {
      out: graph.links.filter((l) => l.source === selectedId),
      in: graph.links.filter((l) => l.target === selectedId),
    };
  }, [graph, selectedId]);

  if (selectedId?.startsWith("synthetic:")) return <Synthetic id={selectedId} />;
  if (!node)
    return (
      <div className="empty">
        <Icon.layers />
        <p>Click any node or connection in the graph and this project's agent explains it.</p>
        <p className="muted small">Drag to move it, double-click to zoom into its neighbourhood, press <kbd>/</kbd> to search.</p>
      </div>
    );

  const status = statuses.get(node.id);
  const hit = view.hits.get(node.id);
  const patch = view.patches.get(node.id)?.patch;
  const touching = view.changes.filter((c) => c.nodeIds.includes(node.id));
  const color = status ? STATUS_COLOR[status] : node.kind === "provider" ? "var(--mint)" : communityColor(node.community);
  const released = view.release?.providerId === node.id ? view.release : undefined;

  return (
    <div className="inspector" key={node.id}>
      <header className="insp-head">
        <span className={`dot lg kind-${node.kind}`} style={{ "--c": color } as React.CSSProperties} />
        <div>
          <h2 className="mono">{node.kind === "provider" ? node.label : shortLabel(node.label)}</h2>
          <p>
            {node.kind} · {node.communityName}
            {status && <b style={{ color }}> · {STATUS_LABEL[status]}</b>}
          </p>
        </div>
        <button className="icon-btn" title="Centre on this node" onClick={() => flyTo([node.id, ...edges.out.map((l) => l.target), ...edges.in.map((l) => l.source)], 2)}>
          <Icon.follow />
        </button>
      </header>

      {node.kind === "provider" ? (
        <>
          <dl className="kv">
            <dt>version</dt>
            <dd className="mono">{released ? `${released.from} → ${released.to}` : node.version}</dd>
            <dt>called by</dt>
            <dd>{edges.in.length} functions</dd>
          </dl>
          <section className="group">
            <h3>Integration map</h3>
            <div className="tree mono">
              <div>{node.label}</div>
              {integrationFiles(graph, node.id).map((f, i, all) => (
                <TreeRow key={f.id} node={f} last={i === all.length - 1} />
              ))}
            </div>
          </section>
        </>
      ) : (
        <dl className="kv">
          <dt>source</dt>
          <dd className="mono">{node.sourceFile ?? node.label}{node.sourceLocation ? `:${node.sourceLocation}` : ""}</dd>
          {hit && (
            <>
              <dt>in this run</dt>
              <dd>
                {hit.role === "change" ? "Rewritten by the migration" : hit.role === "symbol" ? "On the path from the API" : hit.role === "dependent" ? "Depends on changed code, re-verified" : "Runs in the verify stage"}
                {" · "}hop {hit.hop}
              </dd>
            </>
          )}
        </dl>
      )}

      <NodeTrafficStats nodeId={node.id} />
      <Explain target={{ nodeId: node.id }} />

      {touching.length > 0 && (
        <section className="group">
          <h3>Breaking changes that touch it<i>{touching.length}</i></h3>
          <div className="chips">
            {touching.map((c) => <span key={c.id} className="chip mono">{c.before} → {c.after}</span>)}
          </div>
        </section>
      )}

      {patch && (
        <section className="group">
          <h3>Patch</h3>
          <DiffView patch={patch} animate={false} />
        </section>
      )}

      {edges.out.length > 0 && (
        <section className="group">
          <h3>Depends on<i>{edges.out.length}</i></h3>
          {edges.out.map((l) => {
            const n = nodes.get(l.target);
            return n ? <NodeRow key={l.id} node={n} status={statuses.get(n.id)} meta={l.relation.replace(/_/g, " ")} trail={false} /> : null;
          })}
        </section>
      )}
      {edges.in.length > 0 && (
        <section className="group">
          <h3>Used by<i>{edges.in.length}</i></h3>
          {edges.in.map((l) => {
            const n = nodes.get(l.source);
            return n ? <NodeRow key={l.id} node={n} status={statuses.get(n.id)} meta={l.relation.replace(/_/g, " ")} trail={false} /> : null;
          })}
        </section>
      )}
    </div>
  );
}

function TreeRow({ node, last }: { node: GraphNode; last: boolean }) {
  const select = useStore((s) => s.select);
  const setFocus = useStore((s) => s.setFocus);
  return (
    <button onMouseEnter={() => setFocus([node.id])} onMouseLeave={() => setFocus(null)} onClick={() => select(node.id, { fly: true })}>
      <span className="muted">{last ? "└── " : "├── "}</span>
      {node.label}
    </button>
  );
}

function Synthetic({ id }: { id: string }) {
  const view = useStore((s) => s.view);
  const isPr = id.endsWith("pull-request");
  if (isPr && view.pr)
    return (
      <div className="inspector">
        <header className="insp-head">
          <span className="dot lg" style={{ "--c": "var(--violet)" } as React.CSSProperties} />
          <div>
            <h2 className="mono">PR #{view.pr.number}</h2>
            <p>{view.pr.title}</p>
          </div>
        </header>
        <dl className="kv">
          <dt>branch</dt>
          <dd className="mono">{view.pr.branch} → {view.pr.base}</dd>
          <dt>diff</dt>
          <dd className="mono">{view.pr.files} files · +{view.pr.additions} −{view.pr.deletions}</dd>
        </dl>
        <a className="cta cta-violet" href={view.pr.url} target="_blank" rel="noreferrer">
          <span>Open on GitHub</span>
          <Icon.external />
        </a>
      </div>
    );
  if (!isPr && view.docs)
    return (
      <div className="inspector">
        <header className="insp-head">
          <span className="dot lg" style={{ "--c": "var(--blue)" } as React.CSSProperties} />
          <div>
            <h2 className="mono">migration guide</h2>
            <p>{view.docs.title}</p>
          </div>
        </header>
        <div className="cards">
          {view.excerpts.map((ex) => (
            <div key={ex.id} className="card excerpt">
              <span className="eyebrow blue">{ex.section}</span>
              <p>{ex.text}</p>
            </div>
          ))}
        </div>
      </div>
    );
  return null;
}
