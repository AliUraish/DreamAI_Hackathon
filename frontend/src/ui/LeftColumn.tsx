import { useEffect, useMemo, useRef } from "react";
import { fmtClock } from "../lib/derive";
import { useStore } from "../lib/store";
import { communityColor } from "../graph/render";
import { integrationFiles } from "./Inspector";
import { integrationState, useIntegrations, useStatuses } from "./shared";

export function LeftColumn() {
  return (
    <div className="left">
      <Integrations />
      <GraphLegend />
      <ActivityFeed />
    </div>
  );
}

function Integrations() {
  const graph = useStore((s) => s.graph);
  const view = useStore((s) => s.view);
  const select = useStore((s) => s.select);
  const setFocus = useStore((s) => s.setFocus);
  const statuses = useStatuses();
  const integrations = useIntegrations();

  const providers = useMemo(
    () => graph.nodes.filter((n) => n.kind === "provider").map((p) => ({ node: p, files: integrationFiles(graph, p.id) })),
    [graph],
  );
  const changed = [...view.hits.values()].filter((h) => h.role === "change").length;
  // Known to the backend but not in the graph yet: a provider the user just switched to, before any code calls it.
  const codeless = [...integrations.entries()].filter(([nodeId]) => !providers.some((p) => p.node.id === nodeId));

  return (
    <section className="panel integrations">
      <h3 className="panel-title">API integrations<i>{providers.length + codeless.length}</i></h3>
      {providers.length + codeless.length === 0 && <p className="feed-empty mono">none mapped yet</p>}
      {providers.map(({ node, files }) => {
        const released = view.release?.providerId === node.id ? view.release : undefined;
        const status = statuses.get(node.id);
        const stopped = view.done && !view.pr && status !== "ok";
        // The run on screen is fresher than the last dashboard poll; everything else comes from the backend.
        const backend = integrations.get(node.id);
        const idle = backend ? integrationState(backend) : { label: "Healthy", tone: "mint" as const };
        const state = !released ? idle.label : view.pr ? "PR ready" : status === "ok" ? "Verified" : stopped ? "Needs review" : view.stageIndex >= 4 ? "Migrating" : "Breaking";
        const tone = !released ? idle.tone : view.pr || status === "ok" ? "mint" : view.stageIndex >= 4 || stopped ? "amber" : "coral";
        return (
          <button
            key={node.id}
            className="integration"
            onMouseEnter={() => setFocus([node.id, ...files.map((f) => f.id)])}
            onMouseLeave={() => setFocus(null)}
            onClick={() => select(node.id, { fly: true })}
          >
            <span className={`hex ${tone}`} />
            <span className="integration-main">
              <b>{node.label}</b>
              <small className="mono">{[released ? `${released.from} → ${released.to}` : node.version, released && changed ? `${changed} to change` : `${files.length} ${files.length === 1 ? "file" : "files"}`].filter(Boolean).join(" · ")}</small>
            </span>
            <span className={`state ${tone}`}>{state}</span>
          </button>
        );
      })}

      {codeless.map(([nodeId, i]) => (
        <div key={nodeId} className="integration is-static" title="Confirmed as the new provider. It appears in the graph once code calls it.">
          <span className={`hex ${integrationState(i).tone}`} />
          <span className="integration-main">
            <b>{i.name}</b>
            <small className="mono">no call sites yet</small>
          </span>
          <span className={`state ${integrationState(i).tone}`}>{integrationState(i).label}</span>
        </div>
      ))}

    </section>
  );
}

/** Always on screen: which colour is which area of the code, and what the picture encodes. */
function GraphLegend() {
  const count = useStore((s) => new Set(s.graph.nodes.map((n) => n.community)).size);
  return (
    <section className="panel graph-legend">
      <h3 className="panel-title">Communities<i>{count}</i></h3>
      <Legend />
      <h3 className="panel-title">Reading the graph</h3>
      <Reading />
    </section>
  );
}

/** What the picture encodes, stated the way the layout computes it (src/graph/layout.ts), with this project's numbers.
 *  One or two lines each; the tooltip gives the exact rule. */
function Reading() {
  const graph = useStore((s) => s.graph);
  const stats = useMemo(() => {
    const community = new Map(graph.nodes.map((n) => [n.id, n.community]));
    const degree = new Map<string, number>();
    let contains = 0, api = 0, same = 0, cross = 0;
    for (const l of graph.links) {
      degree.set(l.source, (degree.get(l.source) ?? 0) + 1);
      degree.set(l.target, (degree.get(l.target) ?? 0) + 1);
      if (l.relation === "contains") contains++;
      else if (l.relation === "calls_api") api++;
      else if (community.get(l.source) === community.get(l.target)) same++;
      else cross++;
    }
    const hub = graph.nodes.filter((n) => n.kind !== "provider").sort((a, b) => (degree.get(b.id) ?? 0) - (degree.get(a.id) ?? 0))[0];
    return { contains, api, same, cross, hub: hub ? { label: hub.label.split("/").pop()!, links: degree.get(hub.id) ?? 0 } : null };
  }, [graph]);

  const rows: { mark: React.ReactNode; term: string; text: React.ReactNode; rule: string }[] = [
    { mark: <span className="hex mint" />, term: "Hexagon", text: "external API. Dots: files and functions.",
      rule: "Hexagon = a provider node. Round nodes are files (larger) and the functions, classes and types declared in them (smaller)." },
    { mark: <span className="dot" style={{ "--c": "#8b978f", width: 11, height: 11 } as React.CSSProperties} />, term: "Size",
      text: <>links on the node, max 9.{stats.hub && <> Top: <code>{stats.hub.label}</code> <b className="n">{stats.hub.links}</b></>}</>,
      rule: "Radius = base + 0.38 px per link, for the first 9 links. Base: file 5 px, test 4.2 px, function or type 3.6 px. Hexagons are fixed at 12 px." },
    { mark: <i className="len short" />, term: "Short", text: <><code>contains</code> file → its function or type <b className="n">{stats.contains}</b></>,
      rule: "contains links have a target length of 24 px and the strongest pull (0.9), so a file and everything it declares form one tight clump." },
    { mark: <i className="len mid" />, term: "Medium", text: <><code>calls</code> <code>imports</code> in one community <b className="n">{stats.same}</b></>,
      rule: "calls, imports_from and other code links have a target length of 44 px. Inside one community the pull is 0.35, so they hold that length." },
    { mark: <i className="len long" />, term: "Long", text: <><code>calls_api</code> <b className="n">{stats.api}</b> · between two communities <b className="n">{stats.cross}</b></>,
      rule: "calls_api links have a target length of 96 px and hexagons sit outside the code. A link between two communities has a pull of only 0.05, so it stretches as far as the communities are apart. Its length is not a measurement of anything in the code." },
    { mark: <span className="dot" style={{ "--c": "var(--blue)" } as React.CSSProperties} />, term: "Colour", text: "community: tightly linked code.",
      rule: "Colour = Graphify community (Leiden clustering of the code graph), named after the directory most of its nodes live in." },
    { mark: <span className="dot" style={{ "--c": "var(--coral)" } as React.CSSProperties} />, term: "Red / amber", text: "changed API and its callers / what depends on them.",
      rule: "Set by the backend per node while an issue is open. Red = the provider and its call sites (0 hops). Amber = everything downstream of them. Green = patched." },
  ];
  return (
    <dl className="reading">
      {rows.map((r) => (
        <div key={r.term} title={r.rule}>
          <dt>{r.mark}</dt>
          <dd><b>{r.term}</b>{r.text}</dd>
        </div>
      ))}
    </dl>
  );
}

function Legend() {
  const graph = useStore((s) => s.graph);
  const hidden = useStore((s) => s.hiddenCommunities);
  const toggle = useStore((s) => s.toggleCommunity);
  const setFocus = useStore((s) => s.setFocus);
  const groups = useMemo(() => {
    const m = new Map<number, { name: string; ids: string[] }>();
    for (const n of graph.nodes) {
      const g = m.get(n.community) ?? { name: n.communityName, ids: [] };
      g.ids.push(n.id);
      m.set(n.community, g);
    }
    // Areas are named after a directory, and two areas can share one ("services"): tell them apart by their busiest file.
    const names = [...m.values()].map((g) => g.name);
    const degree = new Map<string, number>();
    for (const l of graph.links) for (const id of [l.source, l.target]) degree.set(id, (degree.get(id) ?? 0) + 1);
    for (const [c, g] of m) {
      if (names.filter((n) => n === g.name).length < 2) continue;
      const file = graph.nodes.filter((n) => n.community === c && (n.kind === "file" || n.kind === "test"))
        .sort((a, b) => (degree.get(b.id) ?? 0) - (degree.get(a.id) ?? 0))[0];
      if (file) g.name = `${g.name} · ${file.label.split("/").pop()}`;
    }
    return [...m.entries()].sort((a, b) => b[1].ids.length - a[1].ids.length);
  }, [graph]);
  return (
    <div className="legend">
      {groups.map(([c, g]) => (
        <label key={c} onMouseEnter={() => setFocus(g.ids)} onMouseLeave={() => setFocus(null)}>
          <input type="checkbox" checked={!hidden.has(c)} onChange={() => toggle(c)} />
          <span className="dot" style={{ "--c": communityColor(c) } as React.CSSProperties} />
          <span title={g.name}>{g.name}</span>
          <span className="mono muted">{g.ids.length}</span>
        </label>
      ))}
    </div>
  );
}

function ActivityFeed() {
  const feed = useStore((s) => s.view.feed);
  const started = useStore((s) => s.view.started);
  const listRef = useRef<HTMLOListElement>(null);
  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" });
  }, [feed.length]);

  return (
    <section className="panel feed">
      <h3 className="panel-title">Activity<i>{feed.length}</i></h3>
      {!started || feed.length === 0 ? (
        <p className="feed-empty mono">{started ? "…" : "idle · all contracts unchanged"}</p>
      ) : (
        <ol ref={listRef}>
          {feed.map((f, i) => (
            <li key={`${f.at}-${i}`} className="rise">
              <div className="feed-row">
                <time className="mono">{fmtClock(f.at)}</time>
                <span>{f.msg}</span>
              </div>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
