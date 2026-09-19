// Typed client for the Chowkidaar backend (FastAPI, proxied under /api by vite.config.ts).
// Shapes mirror backend/app/routers/api.py.

import type { Agent, ConfirmRequest, Notice, SimReport, TrafficSnapshot, WireEvent } from "../lib/types";

export type IntegrationStatus =
  | "healthy" | "deprecated" | "breaking" | "migrating" | "migration_ready" | "needs_review" | "unmonitored" | "pending_code";

export interface MigrationCard {
  id: string;
  repo_id: string;
  pr_number: number | null;
  pr_state: "open" | "merged" | "closed" | null;
  review_rounds: number;
  title: string;
  status: "queued" | "running" | "pr_opened" | "ready_local" | "needs_review" | "failed" | "merged" | "closed";
  kind: "repair" | "investigation" | null;
  trigger: "poll" | "provider-release" | "env-change";
  from_version: string | null;
  to_version: string | null;
  pr_url: string | null;
  branch: string | null;
  created_at: string;
  breaking_changes: number;
  affected_files: number;
}

export interface Integration {
  id: string;
  repo_id: string;
  provider: string;
  name: string;
  version: string | null;
  status: IntegrationStatus;
  monitored: boolean;
  last_checked_at: string | null;
  files_affected: number;
  migration: MigrationCard | null;
}

export interface Repo {
  id: string;
  name: string;
  full_name: string | null;
  default_branch: string;
  connected_via: "connector" | "manual" | null;
  open_issues: number;
  integrations: Integration[];
}

export interface Workspace {
  onboarded: boolean;
  name?: string;
  key_prefix?: string;
  connect_command?: string;
  /** Only in the answer to onboarding or a key rotation. Never sent again. */
  api_key?: string;
}

export interface SystemInfo {
  database: "postgres" | "sqlite";
  llm: { provider: string | null; model: string | null };
  github: boolean;
}

export interface Dashboard {
  workspace: Workspace;
  repos: Repo[];
  pending: ConfirmRequest[];
  agents: Agent[];
  unread: number;
  system: SystemInfo;
}

export interface Explanation {
  target: string;
  title: string;
  text: string;
  /** "graph" = worded from graph facts alone; otherwise the model that wrote it. */
  source: string;
  facts: string[];
  ai_available: boolean;
}

export interface EnvVariable {
  name: string;
  provider: string | null;
  platform: string | null;
  has_value: boolean;
  fingerprint: string | null;
  files: string[];
  referenced_in: string[];
}

export interface DemoState {
  available: boolean;
  version?: "v1" | "v2";
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, { ...init, headers: { "content-type": "application/json", ...init?.headers } });
  if (!res.ok) {
    const detail = await res.json().then((b) => b.detail as string).catch(() => "");
    throw new Error(detail || `${init?.method ?? "GET"} /api${path} failed (${res.status})`);
  }
  return res.status === 204 ? (undefined as T) : res.json();
}

const post = <T,>(path: string, body?: unknown) => call<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export const api = {
  dashboard: () => call<Dashboard>("/dashboard"),
  onboard: (name: string) => post<Workspace>("/onboarding", { name }),
  rotateKey: () => post<Workspace>("/workspace/rotate-key"),
  connectRepo: (target: string) =>
    // "owner/repo" clones from GitHub; anything else is a path on the machine the backend runs on.
    post<Repo>("/repos", /^[\w.-]+\/[\w.-]+$/.test(target) ? { full_name: target, background: true } : { local_path: target, background: true }),
  disconnectRepo: (id: string) => call<void>(`/repos/${id}`, { method: "DELETE" }),
  env: async (repoId: string) => (await call<{ variables: EnvVariable[] }>(`/repos/${repoId}/env`)).variables,
  /** "facts" answers at once from the code graph; "ai" has the model word it (seconds) unless it already did. */
  explain: (repoId: string, target: { nodeId: string } | { source: string; target: string }, mode: "facts" | "ai") =>
    post<Explanation>("/explain", { repo_id: repoId, mode, ...("nodeId" in target ? { node_id: target.nodeId } : target) }),
  explanations: (repoId: string) => call<{ done: number; total: number; failed: number; written: number; ai_available: boolean }>(`/repos/${repoId}/explanations`),
  traffic: (repoId: string) => call<TrafficSnapshot>(`/repos/${repoId}/traffic?window=12`),
  simulate: (repoId: string, profile: "steady" | "pressure" | "off") => post<{ profile: string; target: string | null }>(`/repos/${repoId}/traffic/simulate`, { profile }),
  runSimulation: (repoId: string) => post<{ started: boolean }>(`/repos/${repoId}/simulation`),
  lastSimulation: (repoId: string) => call<{ running: boolean; report: SimReport | null; at: string | null }>(`/repos/${repoId}/simulation`),
  auditNow: (repoId: string) => post<{ verdict: string; summary: string; action: string | null }>(`/repos/${repoId}/audit?window=30`),
  startReview: (repoId: string, nodeId: string, prefer?: string) => post<{ migration_id: string }>(`/repos/${repoId}/review`, { node_id: nodeId, prefer }),
  notifications: () => call<Notice[]>("/notifications"),
  markRead: () => post<unknown>("/notifications/read", {}),
  checkAll: () => post<{ status: string; changes: unknown[]; created: boolean }[]>("/check-all"),
  confirm: (id: string, toProvider?: string) => post<{ migration_id: string | null }>(`/env-changes/${id}/confirm`, toProvider ? { to_provider: toProvider } : {}),
  dismiss: (id: string) => post<unknown>(`/env-changes/${id}/dismiss`),
  migrations: (repoId?: string) => call<MigrationCard[]>(`/migrations${repoId ? `?repo_id=${repoId}` : ""}`),
  runEvents: async (migrationId: string) => (await call<{ ui_events: WireEvent[] }>(`/migrations/${migrationId}`)).ui_events,
  demoState: () => call<DemoState>("/demo/state"),
  demoRelease: (announce: boolean) => post<unknown>("/demo/release", { announce }),
  demoReset: () => post<unknown>("/demo/reset"),
};
