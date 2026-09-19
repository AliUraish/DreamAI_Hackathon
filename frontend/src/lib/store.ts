import { create } from "zustand";
import type { DemoState, MigrationCard, Repo, SystemInfo, Workspace } from "../data/api";
import { countApplied, deriveView, type View } from "./derive";
import type { Agent, BaseStatus, ConfirmRequest, GraphData, MapStep, Notice, PipelineEvent, PipelineRun, Recommendation, SimReport, SimStep, TrafficSnapshot, WireEvent } from "./types";

export type CameraCmd =
  | { type: "fit"; nonce: number }
  | { type: "nodes"; ids: string[]; nonce: number; maxZoom?: number }
  | { type: "zoom"; factor: number; nonce: number };

/** connecting: first contact not made yet · live: event stream open · offline: backend unreachable, retrying */
export type Connection = "connecting" | "live" | "offline";

export interface LinkRef {
  source: string;
  target: string;
}

interface State {
  connection: Connection;
  workspace: Workspace | null;
  system: SystemInfo | null;
  /** Shown once, right after onboarding or a rotation; kept for this tab only so "add a project" can print the full command. */
  apiKey: string | null;
  repos: Repo[];
  /** The project on screen. Every project has its own graph, run, and agent. */
  projectId: string | null;
  agents: Agent[];
  notices: Notice[];
  toasts: Notice[];
  /** Questions waiting for the user, across all projects. */
  pending: ConfirmRequest[];
  demo: DemoState;
  lastRun: MigrationCard | null;
  /** Calls per second per node and connection for the project on screen, refreshed every second while there is traffic. */
  traffic: TrafficSnapshot | null;
  /** Better routes the agent found (after a provider key was added); the user decides whether to apply them. */
  recommendations: Recommendation[];
  /** The one-button simulation for the project on screen: its phases while it runs, then its report. */
  sim: { steps: SimStep[]; running: boolean; report: SimReport | null };
  /** Steps of the agent building context for the project on screen; the graph grows when they finish. */
  mapping: MapStep[];
  addingProject: boolean;

  graph: GraphData;
  /** Red/amber/green the backend reports per node, independent of any run on screen. */
  nodeStatus: Map<string, BaseStatus>;
  /** Live runs per project, so a run is not lost while another project is on screen. */
  runs: Record<string, PipelineRun>;
  run: PipelineRun;
  /** ms since run start. -1 = nothing is running for this project. */
  clock: number;
  duration: number;
  playing: boolean;
  view: View;

  selectedId: string | null;
  selectedLink: LinkRef | null;
  hoverId: string | null;
  /** Nodes lit up by hovering UI outside the canvas (a change, a file row, an integration). */
  focusIds: string[] | null;
  hiddenCommunities: Set<number>;
  panelTab: "pipeline" | "inspector";
  /** Camera follows the pipeline until the user takes over by panning or zooming. */
  follow: boolean;
  cameraCmd: CameraCmd | null;
  searchOpen: boolean;

  advance(dtMs: number): void;
  closeRun(): void;
  select(id: string | null, opts?: { fly?: boolean }): void;
  selectLink(link: LinkRef | null): void;
  setHover(id: string | null): void;
  setFocus(ids: string[] | null): void;
  toggleCommunity(c: number): void;
  setTab(t: State["panelTab"]): void;
  setFollow(on: boolean): void;
  flyTo(ids: string[], maxZoom?: number): void;
  fit(): void;
  zoom(factor: number): void;
  setSearch(open: boolean): void;

  setConnection(c: Connection): void;
  setDashboard(d: { workspace: Workspace; repos: Repo[]; pending: ConfirmRequest[]; agents: Agent[]; system: SystemInfo }): void;
  setApiKey(key: string | null): void;
  setDemo(demo: DemoState): void;
  setLastRun(run: MigrationCard | null): void;
  setNotices(notices: Notice[]): void;
  pushNotice(notice: Notice): void;
  dismissToast(id: string): void;
  upsertAgent(agent: Agent): void;
  addPending(request: ConfirmRequest): void;
  resolvePending(id: string): void;
  pushSim(repoId: string, step: SimStep, report?: SimReport): void;
  setSimReport(report: SimReport | null, running: boolean): void;
  setTraffic(repoId: string, traffic: TrafficSnapshot | null): void;
  addRecommendation(repoId: string, r: Recommendation): void;
  dropRecommendation(id: string): void;
  pushMapStep(repoId: string, step: MapStep): void;
  setAddingProject(open: boolean): void;
  openProject(id: string | null): void;
  setGraph(graph: GraphData, status: Map<string, BaseStatus>): void;
  setNodeStatus(status: Map<string, BaseStatus>): void;
  /** Load a finished run from the backend and play it from the start. */
  loadRun(id: string, events: WireEvent[]): void;
  pushEvent(e: WireEvent): void;
}

let nonce = 0;
const durationOf = (run: PipelineRun) => run.events.at(-1)?.at ?? 0;
const emptyRun = (): PipelineRun => ({ id: "live", providerId: "", events: [] });
const EMPTY_GRAPH: GraphData = { repo: "", nodes: [], links: [] };

const storedKey = (() => {
  try { return sessionStorage.getItem("chowkidaar.key"); } catch { return null; }
})();

export const useStore = create<State>((set, get) => ({
  connection: "connecting",
  workspace: null,
  system: null,
  apiKey: storedKey,
  repos: [],
  projectId: null,
  agents: [],
  notices: [],
  toasts: [],
  pending: [],
  demo: { available: false },
  lastRun: null,
  traffic: null,
  recommendations: [],
  sim: { steps: [], running: false, report: null },
  mapping: [],
  addingProject: false,

  graph: EMPTY_GRAPH,
  nodeStatus: new Map(),
  runs: {},
  run: emptyRun(),
  clock: -1,
  duration: 0,
  playing: false,
  view: deriveView(emptyRun(), -1),

  selectedId: null,
  selectedLink: null,
  hoverId: null,
  focusIds: null,
  hiddenCommunities: new Set(),
  panelTab: "pipeline",
  follow: true,
  cameraCmd: null,
  searchOpen: false,

  advance(dtMs) {
    const s = get();
    if (!s.playing || s.clock < 0) return;
    let clock = s.clock + dtMs;
    let playing = true;
    // A live run keeps the clock running while it waits for the next event; a finished one stops at its end.
    if (clock >= s.duration && (s.view.done || s.run.id !== "live")) {
      clock = s.duration;
      playing = false;
    }
    const patch: Partial<State> = { clock, playing };
    if (countApplied(s.run, clock) !== s.view.applied) patch.view = deriveView(s.run, clock);
    set(patch);
  },
  /** Back to monitoring. The run stays on the backend; it can be opened again from the project's history. */
  closeRun() {
    const s = get();
    const runs = { ...s.runs };
    if (s.projectId) delete runs[s.projectId];
    const run = emptyRun();
    set({ runs, run, clock: -1, duration: 0, playing: false, follow: true, selectedId: null, selectedLink: null, panelTab: "pipeline",
          view: deriveView(run, -1), cameraCmd: { type: "fit", nonce: ++nonce } });
  },

  select(id, opts) {
    set({ selectedId: id, selectedLink: null, panelTab: id ? "inspector" : get().panelTab });
    if (id && opts?.fly) get().flyTo([id], 1.7);
  },
  selectLink: (selectedLink) => set({ selectedLink, selectedId: null, panelTab: selectedLink ? "inspector" : get().panelTab }),
  setHover: (hoverId) => {
    if (get().hoverId !== hoverId) set({ hoverId });
  },
  setFocus: (focusIds) => set({ focusIds }),
  toggleCommunity(c) {
    const next = new Set(get().hiddenCommunities);
    if (next.has(c)) next.delete(c);
    else next.add(c);
    set({ hiddenCommunities: next });
  },
  setTab: (panelTab) => set({ panelTab }),
  setFollow: (follow) => set({ follow }),
  flyTo: (ids, maxZoom) => set({ follow: false, cameraCmd: { type: "nodes", ids, maxZoom, nonce: ++nonce } }),
  fit: () => set({ follow: false, cameraCmd: { type: "fit", nonce: ++nonce } }),
  zoom: (factor) => set({ follow: false, cameraCmd: { type: "zoom", factor, nonce: ++nonce } }),
  setSearch: (searchOpen) => set({ searchOpen }),

  // --- backend ----------------------------------------------------------------
  setConnection: (connection) => {
    if (get().connection !== connection) set({ connection });
  },
  setDashboard({ workspace, repos, pending, agents, system }) {
    const s = get();
    // Keep the project on screen if it still exists; otherwise open the first one (or the one named in the URL).
    const wanted = s.projectId ?? new URLSearchParams(location.search).get("project");
    const projectId = (repos ?? []).some((r) => r.id === wanted) ? wanted : repos?.[0]?.id ?? null;
    set({ workspace: workspace ?? { onboarded: true }, repos: repos ?? [], pending: pending ?? [], agents: agents ?? [], system: system ?? null });
    if (projectId !== s.projectId) get().openProject(projectId);
  },
  setApiKey(apiKey) {
    try { apiKey ? sessionStorage.setItem("chowkidaar.key", apiKey) : sessionStorage.removeItem("chowkidaar.key"); } catch { /* private mode */ }
    set({ apiKey });
  },
  setDemo: (demo) => set({ demo }),
  setLastRun: (lastRun) => set({ lastRun }),
  setNotices: (notices) => set({ notices }),
  pushNotice(notice) {
    const s = get();
    set({ notices: [notice, ...s.notices.filter((n) => n.id !== notice.id)].slice(0, 60), toasts: [...s.toasts, notice].slice(-3) });
  },
  dismissToast: (id) => set({ toasts: get().toasts.filter((t) => t.id !== id) }),
  upsertAgent: (agent) => set({ agents: [...get().agents.filter((a) => a.repoId !== agent.repoId), agent] }),
  addPending: (request) => set({ pending: [...get().pending.filter((p) => p.id !== request.id), request] }),
  resolvePending: (id) => set({ pending: get().pending.filter((p) => p.id !== id) }),
  pushSim(repoId, step, report) {
    if (repoId !== get().projectId) return;
    const prev = get().sim;
    const steps = step.phase === "steady" ? [step] : [...prev.steps, step];
    set({ sim: { steps, running: step.phase !== "report", report: report ?? (step.phase === "steady" ? null : prev.report) } });
  },
  setSimReport: (report, running) => set({ sim: { steps: get().sim.steps, running: running || get().sim.running, report: get().sim.report ?? report } }),
  setTraffic(repoId, traffic) {
    if (repoId === get().projectId) set({ traffic: traffic && traffic.source !== "none" ? traffic : null });
  },
  addRecommendation(repoId, r) {
    if (repoId === get().projectId) set({ recommendations: [...get().recommendations.filter((x) => x.id !== r.id), r] });
  },
  dropRecommendation: (id) => set({ recommendations: get().recommendations.filter((r) => r.id !== id) }),
  pushMapStep(repoId, step) {
    const s = get();
    // A project that starts mapping while nothing else is open takes the screen: that is the moment after onboarding.
    if (!s.projectId || (step.phase === "env" && s.addingProject)) {
      set({ addingProject: false });
      get().openProject(repoId);
    }
    if (get().projectId === repoId) set({ mapping: step.phase === "env" ? [step] : [...get().mapping, step] });
  },
  setAddingProject: (addingProject) => set({ addingProject }),

  openProject(projectId) {
    const s = get();
    if (projectId === s.projectId) return;
    const run = (projectId && s.runs[projectId]) || emptyRun();
    const duration = durationOf(run);
    // A run that went on while another project was on screen is shown where it stands now.
    const clock = run.events.length ? duration : -1;
    set({ projectId, graph: EMPTY_GRAPH, nodeStatus: new Map(), mapping: [], lastRun: null, traffic: null, recommendations: [], sim: { steps: [], running: false, report: null }, run, duration, clock, playing: run.events.length > 0,
          view: deriveView(run, clock), selectedId: null, selectedLink: null, hoverId: null, focusIds: null, panelTab: "pipeline", follow: true,
          hiddenCommunities: new Set() });
    const url = new URL(location.href);
    if (projectId) url.searchParams.set("project", projectId);
    else url.searchParams.delete("project");
    history.replaceState(null, "", url);
  },
  // A new graph object restarts the grow animation; a status refresh must not.
  setGraph: (graph, nodeStatus) => set({ graph, nodeStatus }),
  setNodeStatus: (nodeStatus) => set({ nodeStatus }),

  loadRun(id, events) {
    // Played back at the pace it really happened (`offsetMs` from the backend), so a check that ran for three seconds is seen
    // running for three seconds. Long silences (the model writing code) are shortened, never the steps themselves.
    const MAX_GAP = 14000;
    const expanded: (WireEvent & { offsetMs?: number })[] = [];
    const seenRunning = new Set<string>();
    for (const e of events as (WireEvent & { offsetMs?: number })[]) {
      if (e.t === "check") {
        const key = `${e.check.id}:${e.check.phase}`;
        if (e.check.status === "running") seenRunning.add(key);
        else if (!seenRunning.has(key)) {
          // Older runs only stored the result: show the command running first, for as long as it took.
          const took = Math.max(1200, Math.min(9000, e.check.durationMs ?? 3200));
          expanded.push({ ...e, msg: undefined, check: { ...e.check, status: "running", detail: undefined, output: null }, offsetMs: (e.offsetMs ?? 0) - took });
        }
      }
      expanded.push(e);
    }
    let shift = 0, lastOffset = 0;
    const stamped = expanded.reduce<PipelineEvent[]>((log, e) => {
      const prev = log.at(-1);
      const pace = prev ? paceAfter(prev, e as PipelineEvent) : 0;
      let at = prev ? prev.at + pace : 0;
      if (e.offsetMs !== undefined && prev) {
        const gap = Math.max(0, e.offsetMs - lastOffset);
        if (gap > MAX_GAP) shift += gap - MAX_GAP;
        at = Math.max(at, e.offsetMs - shift);
      }
      lastOffset = Math.max(lastOffset, e.offsetMs ?? lastOffset);
      const { offsetMs: _drop, ...rest } = e;
      log.push({ ...rest, at } as PipelineEvent);
      return log;
    }, []);
    const release = stamped.find((e) => e.t === "release");
    const run: PipelineRun = { id, providerId: release?.t === "release" ? release.providerId : "", events: stamped };
    set({ run, duration: durationOf(run), clock: 0, playing: true, follow: true, selectedId: null, selectedLink: null, panelTab: "pipeline", view: deriveView(run, 0) });
  },
  pushEvent(e) {
    const s0 = get();
    const repoId = e.repoId ?? s0.projectId ?? "";
    const onScreen = repoId === s0.projectId;
    let log = onScreen ? s0.run : s0.runs[repoId] ?? emptyRun();
    // A "detect" on top of an existing log means the agent started another run: begin a fresh one.
    const fresh = (e.t === "stage" && e.stage === "detect" && log.events.length > 0) || log.id !== "live";
    if (fresh) log = emptyRun();
    // Stamped on arrival, but never closer together than the pacing below: a graph walk arrives as one
    // burst and should still play out hop by hop. The clock runs behind the log head until it catches up.
    const prev = log.events.at(-1);
    const floor = onScreen && !fresh ? s0.clock : 0;
    const at = Math.max(prev ? prev.at + paceAfter(prev, e as PipelineEvent) : 0, floor);
    const run: PipelineRun = { ...log, providerId: e.t === "release" ? e.providerId : log.providerId, events: [...log.events, { ...e, at } as PipelineEvent] };
    const runs = { ...s0.runs, [repoId]: run };
    if (!onScreen) return set({ runs });
    const clock = fresh ? 0 : Math.max(0, s0.clock);
    set({ runs, run, duration: at, clock, playing: true, view: deriveView(run, clock),
          ...(fresh ? { selectedId: null, selectedLink: null, panelTab: "pipeline" as const, follow: true } : {}) });
  },
}));

/** Minimum gap, in ms, between two consecutive live events. */
function paceAfter(prev: PipelineEvent, next: PipelineEvent): number {
  if (next.t === "hit") return prev.t === "hit" && prev.hit.hop === next.hit.hop ? 170 : 600;
  if (next.t === "check") return 140;
  if (next.t === "change" || next.t === "excerpt") return 700;
  if (next.t === "stage") return 900;
  return 300;
}
