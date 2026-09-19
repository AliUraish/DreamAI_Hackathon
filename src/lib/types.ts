// ---------------------------------------------------------------------------
// Graph — mirrors graphify's graph.json (NetworkX node-link format), as served
// by GET /api/graph. The backend adds provider nodes (file_type: "provider") and
// "calls_api" links, since graphify itself does not model outbound HTTP calls,
// and only sends the maintained API pipelines, never the whole repository.
// ---------------------------------------------------------------------------

export type NodeKind = "provider" | "file" | "function" | "class" | "test" | "doc" | "pr";

export interface GraphNode {
  id: string;
  label: string;
  kind: NodeKind;
  community: number;
  communityName: string;
  sourceFile?: string;
  sourceLocation?: string;
  /** Provider nodes only. */
  version?: string;
  /** Synthetic nodes (migration guide, PR) stay hidden until the run reveals them. */
  synthetic?: boolean;
  /** When the node joins the picture as the graph grows: 0 provider, 1 call sites, then outwards hop by hop. */
  order?: number;
}

/** What the backend knows about a node with no run on screen: red where an open issue reaches. */
export type BaseStatus = "healthy" | "breaking" | "affected" | "patched";

export interface GraphLink {
  id: string;
  source: string;
  target: string;
  relation: string;
}

export interface GraphData {
  repo: string;
  repoId?: string;
  /** Set when the graph is a slice: how many nodes the whole repo graph has. */
  repoNodesTotal?: number;
  nodes: GraphNode[];
  links: GraphLink[];
}

// ---------------------------------------------------------------------------
// Pipeline run — an append-only event log. The UI is a pure function of
// (events, clock), which is what makes the timeline scrubbable. The live SSE
// stream and a replayed past run (GET /api/migrations/{id}) share this shape.
// ---------------------------------------------------------------------------

export type StageId = "detect" | "diff" | "trace" | "docs" | "patch" | "verify" | "pr";

export type ChangeKind = "endpoint" | "request" | "response" | "semantic";

export interface BreakingChange {
  id: string;
  kind: ChangeKind;
  /** What happened, in the backend's words ("field renamed", "provider switched"). */
  label?: string;
  before: string;
  after: string;
  where: string;
  note?: string | null;
  /** Graph nodes that touch this part of the contract. */
  nodeIds: string[];
}

/** change: file gets patched · symbol: on the path · dependent: at risk, re-verified · test: runs in verify */
export type HitRole = "change" | "symbol" | "dependent" | "test";

export interface TraceHit {
  nodeId: string;
  /** Node the trace arrived from (the provider for hop 1). */
  from: string;
  hop: number;
  role: HitRole;
}

export interface DocExcerpt {
  id: string;
  section: string;
  text: string;
  changeIds: string[];
  nodeIds: string[];
}

export interface DiffLine {
  t: " " | "+" | "-";
  s: string;
}

export interface FilePatch {
  nodeId: string;
  path: string;
  additions: number;
  deletions: number;
  hunks: { header: string; lines: DiffLine[] }[];
}

export type CheckStatus = "running" | "failed" | "passed";

export interface Check {
  id: string;
  group: "tests" | "types" | "build";
  name: string;
  /** "baseline" = run against v2 before the patch (expected red); "patched" = after. */
  phase: "baseline" | "patched";
  status: CheckStatus;
  detail?: string;
  nodeId?: string;
  /** The command that produced it, how long it really took, and the end of its output. */
  cmd?: string;
  durationMs?: number | null;
  output?: string[] | null;
}

export interface PullRequest {
  number: number;
  title: string;
  branch: string;
  base: string;
  url: string;
  files: number;
  additions: number;
  deletions: number;
}

export interface ReviewComment {
  id: string;
  author: string;
  body: string;
  path?: string | null;
  line?: number | null;
}

export interface ReviewUpdate {
  commit: string;
  summary: string;
  checks: string;
  files: string[];
}

interface Base {
  /** ms from run start. */
  at: number;
  /** "pressure" runs reuse the seven stages with different names: detect, measure, trace, review, patch, verify, PR. */
  kind?: "pressure";
  /** Which repository and run the event belongs to (several agents can be working at once). */
  repoId?: string;
  runId?: string;
  /** Line for the activity feed. Events without one are silent. */
  msg?: string;
}

export type PipelineEvent =
  | (Base & { t: "stage"; stage: StageId })
  | (Base & { t: "release"; providerId: string; from: string; to: string; source: string })
  | (Base & { t: "change"; change: BreakingChange })
  | (Base & { t: "hit"; hit: TraceHit })
  | (Base & { t: "docs"; title: string; url: string })
  | (Base & { t: "excerpt"; excerpt: DocExcerpt })
  | (Base & { t: "patch.start"; nodeId: string })
  | (Base & { t: "patch.done"; patch: FilePatch })
  | (Base & { t: "check"; check: Check })
  | (Base & { t: "pr"; pr: PullRequest })
  | (Base & { t: "pressure"; nodeId: string; metrics: NodeTraffic; source: string })
  | (Base & { t: "review"; review: Review })
  | (Base & { t: "pr.comment"; comment: ReviewComment })
  | (Base & { t: "pr.updated"; update: ReviewUpdate })
  | (Base & { t: "pr.merged"; merged: { verified: boolean; checks: string; base: string; at: string } })
  | (Base & { t: "done" });

type DistributiveOmit<T, K extends PropertyKey> = T extends unknown ? Omit<T, K> : never;
/** A PipelineEvent as the backend sends it: no `at`, the UI stamps arrival time. */
export type WireEvent = DistributiveOmit<PipelineEvent, "at">;

/** A question the backend will not answer for the user (an environment change that looks like a provider switch). */
export interface ConfirmRequest {
  id: string;
  repoId?: string;
  kind: "provider-switched" | "env-renamed";
  title: string;
  body: string;
  fromProviderId?: string | null;
  toProviderId?: string | null;
  needsProviderChoice: boolean;
  providerOptions?: { id: string; name: string }[];
}

export interface Agent {
  id: string;
  repoId: string;
  repoName: string;
  status: "idle" | "working" | "waiting";
  task: string | null;
  detail: string | null;
  migrationId: string | null;
  startedAt: string | null;
  memories: number;
}

export interface Notice {
  id: string;
  repoId: string | null;
  level: "info" | "success" | "warning" | "action";
  title: string;
  body: string;
  link: string | null;
  read: boolean;
  createdAt: string;
}

export interface NodeTraffic { rps: number; mean_ms: number; p50_ms: number; p95_ms: number; errors: number; budget: number; load: number; calls: number }

/** Calls per second on every node and connection over the last few seconds. `simulated` is never shown as if it were measured. */
export interface TrafficSnapshot {
  source: "live" | "simulated" | "none";
  window: number;
  nodes: Record<string, NodeTraffic>;
  links: Record<string, { rps: number; mean_ms: number }>;
  simulation?: { profile: string; target: string | null } | null;
  /** The standing recommendation: where adding a node would help right now, recomputed every few seconds from this traffic. */
  suggestions?: Suggestion[];
}

export interface Suggestion {
  nodeId: string;
  label: string;
  prefer: string;
  kind: "split" | "route";
  title: string;
  summary: string;
  new_nodes: { id: string; label: string; from: string; provider?: string }[];
  before: Outcome & { rps: number; budget: number };
  after: Outcome;
  gain: { p95_pct: number; capacity_pct: number };
}

export interface Outcome { p95_ms: number; load: number; capacity_rps: number; saturated: boolean }

export interface ReviewOption {
  id: string;
  kind: "split" | "route" | "limit";
  title: string;
  summary: string;
  new_nodes: { id: string; label: string; from: string; provider?: string }[];
  after: Outcome;
  gain: { p95_pct: number; capacity_pct: number };
}

/** The agent's answer to a node under pressure. Every number comes from the queueing simulation, not from the model. */
export interface Review {
  nodeId: string;
  label: string;
  before: Outcome & { rps: number; service_ms: number; budget: number };
  options: ReviewOption[];
  recommended: string;
  diagnosis: string;
  why: string;
  risks: string[];
  source: "live" | "simulated";
  decidedBy: string;
}

/** What a one-button simulation found. Numbers are from the traffic simulation and the queueing model, never from the LLM. */
export interface SimReport {
  headline: string;
  source: "simulated";
  metrics: { node: string; label: string; rps: number; p95_ms: number; load: number; errors: number; budget: number; calm_load: number }[];
  good: { text: string; nodeIds: string[] }[];
  bad: { text: string; nodeIds: string[]; severity: "pressure" | "watch" | "errors" }[];
  goodText: string[];
  badText: string[];
  advice: string;
  recommendation: (Omit<Recommendation, "id" | "providerName" | "env"> & { new_nodes: ReviewOption["new_nodes"]; alternatives: { title: string; p95_ms: number }[] }) | null;
  writtenBy: string;
}

export interface SimStep { phase: "steady" | "ramp" | "measure" | "report"; msg: string }

export interface Recommendation {
  id: string;
  nodeId: string;
  label: string;
  title: string;
  summary: string;
  prefer: string;
  providerName: string;
  env: string | null;
  before?: Outcome;
  after?: Outcome;
  gain?: { p95_pct: number; capacity_pct: number };
}

/** A step of the agent building its context for a repository; the graph grows with it. */
export interface MapStep {
  phase: "env" | "env.done" | "scan" | "pipelines" | "done";
  msg: string;
}

/** Stream messages that are not part of a run's log. */
export type ControlEvent =
  | { t: "confirm.request"; repoId?: string; request: ConfirmRequest; msg?: string }
  | { t: "confirm.resolved"; repoId?: string; id: string; decision: "confirmed" | "dismissed" }
  | { t: "agent"; repoId: string; agent: Agent }
  | { t: "notify"; repoId?: string; notification: Notice }
  | { t: "traffic"; repoId: string; traffic: TrafficSnapshot }
  | { t: "recommend"; repoId: string; recommendation: Recommendation }
  | ({ t: "sim"; repoId: string; report?: SimReport } & SimStep)
  | ({ t: "map"; repoId: string } & MapStep);

export interface PipelineRun {
  id: string;
  providerId: string;
  events: PipelineEvent[];
}

export const STAGES: { id: StageId; label: string; blurb: string }[] = [
  { id: "detect", label: "Detect", blurb: "Provider ships a new version" },
  { id: "diff", label: "Diff", blurb: "Old contract vs new contract" },
  { id: "trace", label: "Trace", blurb: "Walk the code graph for the blast radius" },
  { id: "docs", label: "Docs", blurb: "Read the migration guide" },
  { id: "patch", label: "Patch", blurb: "Rewrite only the affected files" },
  { id: "verify", label: "Verify", blurb: "Tests, types, build" },
  { id: "pr", label: "PR", blurb: "Open a pull request" },
];
