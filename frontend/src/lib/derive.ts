import type {
  BreakingChange,
  Check,
  DocExcerpt,
  FilePatch,
  PipelineRun,
  PullRequest,
  NodeTraffic,
  Review,
  ReviewComment,
  ReviewUpdate,
  StageId,
  TraceHit,
} from "./types";
import { STAGES } from "./types";

type At<T> = T & { at: number };

export interface PatchState {
  status: "patching" | "done";
  startedAt: number;
  doneAt?: number;
  patch?: FilePatch;
}

export interface View {
  /** Number of events folded in. The view only changes identity when this does. */
  applied: number;
  started: boolean;
  done: boolean;
  stage: StageId | null;
  stageIndex: number;
  stageStart: Partial<Record<StageId, number>>;
  release?: At<{ providerId: string; from: string; to: string; source: string }>;
  changes: At<BreakingChange>[];
  hits: Map<string, At<TraceHit>>;
  docs?: At<{ title: string; url: string }>;
  excerpts: At<DocExcerpt>[];
  patches: Map<string, PatchState>;
  checks: At<Check>[];
  pr?: At<PullRequest>;
  /** Set when the run is about a node under pressure rather than an API change. */
  kind: "change" | "pressure";
  pressure?: At<{ nodeId: string; metrics: NodeTraffic; source: string }>;
  review?: At<Review>;
  comments: At<ReviewComment>[];
  updates: At<ReviewUpdate>[];
  merged?: At<{ verified: boolean; checks: string; base: string }>;
  feed: { at: number; msg: string; stage: StageId | null }[];
}

export function countApplied(run: PipelineRun, clock: number): number {
  if (clock < 0) return 0;
  let n = 0;
  while (n < run.events.length && run.events[n].at <= clock) n++;
  return n;
}

export function deriveView(run: PipelineRun, clock: number): View {
  const applied = countApplied(run, clock);
  const v: View = {
    applied,
    started: clock >= 0,
    done: false,
    stage: null,
    stageIndex: -1,
    stageStart: {},
    changes: [],
    hits: new Map(),
    excerpts: [],
    patches: new Map(),
    checks: [],
    kind: "change",
    comments: [],
    updates: [],
    feed: [],
  };
  for (let i = 0; i < applied; i++) {
    const e = run.events[i];
    switch (e.t) {
      case "stage":
        v.stage = e.stage;
        v.stageIndex = STAGES.findIndex((s) => s.id === e.stage);
        v.stageStart[e.stage] = e.at;
        break;
      case "release":
        v.release = { providerId: e.providerId, from: e.from, to: e.to, source: e.source, at: e.at };
        break;
      case "change":
        v.changes.push({ ...e.change, at: e.at });
        break;
      case "hit":
        v.hits.set(e.hit.nodeId, { ...e.hit, at: e.at });
        break;
      case "docs":
        v.docs = { title: e.title, url: e.url, at: e.at };
        break;
      case "excerpt":
        v.excerpts.push({ ...e.excerpt, at: e.at });
        break;
      case "patch.start":
        v.patches.set(e.nodeId, { status: "patching", startedAt: e.at });
        break;
      case "patch.done": {
        const prev = v.patches.get(e.patch.nodeId);
        v.patches.set(e.patch.nodeId, { status: "done", startedAt: prev?.startedAt ?? e.at, doneAt: e.at, patch: e.patch });
        break;
      }
      case "check": {
        const idx = v.checks.findIndex((c) => c.id === e.check.id && c.phase === e.check.phase);
        if (idx >= 0) v.checks[idx] = { ...e.check, at: e.at };
        else v.checks.push({ ...e.check, at: e.at });
        break;
      }
      case "pr":
        v.pr = { ...e.pr, at: e.at };
        break;
      case "pressure":
        v.pressure = { nodeId: e.nodeId, metrics: e.metrics, source: e.source, at: e.at };
        break;
      case "review":
        v.review = { ...e.review, at: e.at };
        break;
      case "pr.comment":
        v.comments.push({ ...e.comment, at: e.at });
        break;
      case "pr.updated":
        v.updates.push({ ...e.update, at: e.at });
        break;
      case "pr.merged":
        v.merged = { ...e.merged, at: e.at };
        break;
      case "done":
        v.done = true;
        break;
    }
    if (e.kind === "pressure") v.kind = "pressure";
    if (e.msg) v.feed.push({ at: e.at, msg: e.msg, stage: v.stage });
  }
  return v;
}

export interface StageWindow {
  id: StageId;
  start: number | null;
  end: number | null;
}

/** Start/end of every stage seen so far in the log. Unseen stages have null bounds. */
export function stageWindows(run: PipelineRun, duration: number): StageWindow[] {
  const starts = new Map<StageId, number>();
  for (const e of run.events) if (e.t === "stage" && !starts.has(e.stage)) starts.set(e.stage, e.at);
  return STAGES.map((s, i) => {
    const start = starts.get(s.id) ?? null;
    let end: number | null = null;
    if (start !== null) {
      for (let j = i + 1; j < STAGES.length && end === null; j++) end = starts.get(STAGES[j].id) ?? null;
      end ??= duration;
    }
    return { id: s.id, start, end };
  });
}

/** Path from a traced node back to the provider, following each hit's `from`. */
export function pathToProvider(view: View, nodeId: string): string[] {
  const out = [nodeId];
  let cur = view.hits.get(nodeId);
  let guard = 0;
  while (cur && guard++ < 32) {
    out.push(cur.from);
    cur = view.hits.get(cur.from);
  }
  return out;
}

export function fmtClock(ms: number): string {
  const s = Math.max(0, ms) / 1000;
  const m = Math.floor(s / 60);
  return `${String(m).padStart(2, "0")}:${(s % 60).toFixed(1).padStart(4, "0")}`;
}
