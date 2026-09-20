import { useStore } from "../lib/store";
import type { BaseStatus, ControlEvent, GraphData, GraphNode, NodeKind, WireEvent } from "../lib/types";
import { api } from "./api";

// The app has one data source: the Chowkidaar backend, proxied under /api.
//
//   GET /api/graph       graphify graph.json (node-link) restricted to the maintained API
//                        pipelines, plus provider nodes { file_type: "provider", version }
//                        joined to their callers by links with relation "calls_api"
//   GET /api/events      text/event-stream; each message is one PipelineEvent without `at`,
//                        or a ControlEvent (confirm.request / confirm.resolved)
//   GET /api/dashboard   repositories, integration statuses, questions waiting for the user
//
// Nothing is bundled: with the backend down the page shows an empty graph and says so.

interface GraphifyNode {
  id: string;
  label?: string;
  community?: number | null;
  community_name?: string;
  file_type?: string;
  source_file?: string;
  source_location?: string;
  version?: string;
  order?: number;
  status?: BaseStatus;
}
interface GraphifyJson {
  graph?: { repo?: string; repo_id?: string; repo_nodes_total?: number };
  nodes: GraphifyNode[];
  links?: { source: string; target: string; relation?: string }[];
  edges?: { source: string; target: string; relation?: string }[];
}

function kindOf(n: GraphifyNode): NodeKind {
  const label = n.label ?? n.id;
  if (n.file_type === "provider") return "provider";
  if (/\.(test|spec)\.[a-z]+$/.test(label)) return "test";
  if (/\.[a-z]{1,4}$/i.test(label)) return "file";
  if (label.endsWith(")")) return "function";
  return "class";
}

export function adaptGraphify(json: GraphifyJson, repo: string): GraphData {
  const nodes: GraphNode[] = json.nodes.map((n) => ({
    id: n.id,
    label: n.label ?? n.id,
    kind: kindOf(n),
    community: n.community ?? 0,
    communityName: n.community_name ?? `community ${n.community ?? 0}`,
    sourceFile: n.source_file,
    sourceLocation: n.source_location,
    version: n.version,
    order: n.order ?? 1,
  }));
  const ids = new Set(nodes.map((n) => n.id));
  const links = (json.links ?? json.edges ?? [])
    .filter((l) => ids.has(l.source) && ids.has(l.target))
    .map((l) => ({ id: `${l.source}->${l.target}`, source: l.source, target: l.target, relation: l.relation ?? "references" }));
  return { repo: json.graph?.repo ?? repo, repoId: json.graph?.repo_id, repoNodesTotal: json.graph?.repo_nodes_total, nodes, links };
}

export async function loadGraph(): Promise<void> {
  const projectId = useStore.getState().projectId;
  if (!projectId) return;
  const res = await fetch(`/api/graph?repo_id=${projectId}`);
  if (!res.ok) throw new Error(`GET /api/graph ${res.status}`);
  const json = (await res.json()) as GraphifyJson;
  const store = useStore.getState();
  if (store.projectId !== projectId) return; // the user moved on while this was in flight
  const next = adaptGraphify(json, "");
  const status = new Map(json.nodes.map((n) => [n.id, n.status ?? "healthy"] as const));
  const cur = store.graph;
  const same = cur.repoId === next.repoId && cur.nodes.length === next.nodes.length && cur.links.length === next.links.length &&
    cur.nodes.every((n, i) => n.id === next.nodes[i].id && n.version === next.nodes[i].version);
  // Same shape: only recolour. A new graph object would replay the grow animation.
  if (same) store.setNodeStatus(status);
  else store.setGraph(next, status);
}

/** Workspace, projects, agents, questions, and everything about the project on screen. */
export async function refresh(): Promise<void> {
  const store = useStore.getState();
  const [dashboard, demo] = await Promise.all([api.dashboard(), api.demoState()]);
  store.setDashboard(dashboard);
  store.setDemo(demo);
  const projectId = useStore.getState().projectId;
  if (!projectId) return;
  const [migrations, traffic, simulation] = await Promise.all([api.migrations(projectId), api.traffic(projectId).catch(() => null),
    api.lastSimulation(projectId).catch(() => null), loadGraph()]);
  if (useStore.getState().projectId !== projectId) return;
  if (simulation) useStore.getState().setSimReport(simulation.report, simulation.running);
  useStore.getState().setLastRun(migrations[0] ?? null);
  // The stream sends traffic every second while there is any; this only clears it when it stops.
  if (!traffic || traffic.source === "none") useStore.getState().setTraffic(projectId, null);
}

export async function replayRun(migrationId: string): Promise<void> {
  useStore.getState().loadRun(migrationId, await api.runEvents(migrationId));
}

function openStream(): void {
  const store = useStore.getState();
  const stream = new EventSource("/api/events");
  let queue = Promise.resolve();
  stream.onopen = () => {
    store.setConnection("live");
    refresh().catch(() => {});
  };
  // EventSource reconnects by itself; this only reports the gap.
  stream.onerror = () => store.setConnection("offline");
  stream.onmessage = (m) => {
    const event = JSON.parse(m.data) as WireEvent | ControlEvent;
    queue = queue.then(async () => {
      switch (event.t) {
        case "confirm.request": return store.addPending(event.request);
        case "confirm.resolved": return store.resolvePending(event.id);
        case "agent": return store.upsertAgent(event.agent);
        case "notify": return store.pushNotice(event.notification);
        case "traffic": return store.setTraffic(event.repoId, event.traffic);
        case "recommend": return store.addRecommendation(event.repoId, event.recommendation);
        case "sim": return store.pushSim(event.repoId, { phase: event.phase, msg: event.msg }, event.report, event.predictions);
        case "map":
          store.pushMapStep(event.repoId, { phase: event.phase, msg: event.msg });
          // Context arrives in steps; the picture is drawn when the pipelines are traced.
          if (event.phase === "pipelines" || event.phase === "done") await refresh().catch(() => {});
          return;
      }
      store.pushEvent(event);
      if (event.t === "done" || event.t === "pr" || event.t === "pr.merged" || (event.t === "stage" && event.stage === "detect")) refresh().catch(() => {});
    });
  };
}

export async function connect(): Promise<void> {
  try {
    await refresh();
    useStore.getState().setNotices(await api.notifications());
    // A reload in the middle of a run picks it up again from the backend's log.
    const last = useStore.getState().lastRun;
    if (last && (last.status === "running" || last.status === "queued")) await replayRun(last.id).catch(() => {});
  } catch {
    useStore.getState().setConnection("offline");
  }
  openStream();
  // Statuses also move without a run (polls, merges, another tab answering a question).
  setInterval(() => refresh().then(() => useStore.getState().setConnection("live")).catch(() => useStore.getState().setConnection("offline")), 5000);
}

// Opening another project loads its graph, which grows in from the API outwards.
useStore.subscribe((s, prev) => {
  if (s.projectId !== prev.projectId && s.projectId) refresh().catch(() => {});
});
