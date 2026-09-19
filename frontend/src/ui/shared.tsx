import { useMemo, type ReactNode, type SVGProps } from "react";
import { pathToProvider } from "../lib/derive";
import { useStore } from "../lib/store";
import type { Integration } from "../data/api";
import type { GraphNode } from "../lib/types";
import { communityColor, computeStatuses, shortLabel, STATUS_COLOR, type Status } from "../graph/render";

// --- icons ------------------------------------------------------------------
const Svg = ({ children, ...p }: SVGProps<SVGSVGElement>) => (
  <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden {...p}>
    {children}
  </svg>
);
export const Icon = {
  play: () => <Svg><path d="M5 3.5v9l7-4.5z" fill="currentColor" stroke="none" /></Svg>,
  pause: () => <Svg><path d="M5 3.5v9M11 3.5v9" strokeWidth="2.2" /></Svg>,
  restart: () => <Svg><path d="M3 8a5 5 0 1 0 1.6-3.7M3 2.5v3h3" /></Svg>,
  prev: () => <Svg><path d="M10.5 3.5 6 8l4.5 4.5" /></Svg>,
  next: () => <Svg><path d="M5.5 3.5 10 8l-4.5 4.5" /></Svg>,
  plus: () => <Svg><path d="M8 3.5v9M3.5 8h9" /></Svg>,
  minus: () => <Svg><path d="M3.5 8h9" /></Svg>,
  fit: () => <Svg><path d="M2.5 6V2.5H6M10 2.5h3.5V6M13.5 10v3.5H10M6 13.5H2.5V10" /></Svg>,
  follow: () => <Svg><circle cx="8" cy="8" r="2" /><path d="M8 1.5v3M8 11.5v3M1.5 8h3M11.5 8h3" /></Svg>,
  search: () => <Svg><circle cx="7" cy="7" r="4" /><path d="m10 10 3.5 3.5" /></Svg>,
  check: () => <Svg><path d="m3.5 8.5 3 3 6-7" /></Svg>,
  x: () => <Svg><path d="m4 4 8 8M12 4l-8 8" /></Svg>,
  arrow: () => <Svg><path d="M3 8h10M9.5 4.5 13 8l-3.5 3.5" /></Svg>,
  external: () => <Svg><path d="M6.5 3.5h-3v9h9v-3M9 3h4v4M13 3 7.5 8.5" /></Svg>,
  layers: () => <Svg><path d="m8 2 6 3-6 3-6-3zM2 8l6 3 6-3M2 11l6 3 6-3" /></Svg>,
  bell: () => <Svg><path d="M4 11.5V7a4 4 0 0 1 8 0v4.5l1 1.5H3zM6.5 13.5a1.5 1.5 0 0 0 3 0" /></Svg>,
  chevron: () => <Svg><path d="m4.5 6.5 3.5 3.5 3.5-3.5" /></Svg>,
  branch: () => <Svg><circle cx="4.5" cy="3.5" r="1.5" /><circle cx="4.5" cy="12.5" r="1.5" /><circle cx="11.5" cy="5.5" r="1.5" /><path d="M4.5 5v6M11.5 7c0 2.5-7 1.5-7 4" /></Svg>,
};

export function Spinner() {
  return <span className="spinner" aria-label="running" />;
}

// --- status -----------------------------------------------------------------
export function useStatuses(): Map<string, Status> {
  const view = useStore((s) => s.view);
  const graph = useStore((s) => s.graph);
  const byId = useNodeMap(graph.nodes);
  return useMemo(() => computeStatuses(view, byId), [view, byId]);
}

export function useNodeMap(nodes: GraphNode[]): Map<string, GraphNode> {
  return useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes]);
}

export const STATUS_LABEL: Record<Status, string> = {
  breaking: "breaking",
  fail: "failing",
  risk: "at risk",
  patching: "patching",
  ok: "ok",
  pending: "pending",
};

// --- integrations, as the backend reports them ---------------------------------
/** provider node id ("provider:openai") -> the backend's integration record. */
export function useIntegrations(): Map<string, Integration> {
  const repos = useStore((s) => s.repos);
  return useMemo(() => new Map(repos.flatMap((r) => r.integrations).map((i) => [`provider:${i.provider}`, i])), [repos]);
}

export type Tone = "mint" | "amber" | "coral" | "blue" | "muted";

export function integrationState(i: Integration): { label: string; tone: Tone } {
  switch (i.status) {
    case "healthy": return { label: "Healthy", tone: "mint" };
    case "deprecated": return { label: "Deprecated", tone: "amber" };
    case "breaking": return { label: "Breaking", tone: "coral" };
    case "migrating": return { label: "Migrating", tone: "amber" };
    case "migration_ready": return { label: i.migration?.pr_url ? "PR ready" : "Branch ready", tone: "mint" };
    case "needs_review": return { label: "Needs review", tone: "amber" };
    case "pending_code": return { label: "Awaiting code", tone: "blue" };
    case "unmonitored": return { label: "Not polled", tone: "muted" };
  }
}

export function ago(iso: string | null): string {
  if (!iso) return "never";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  return s < 60 ? "just now" : s < 3600 ? `${Math.floor(s / 60)}m ago` : s < 86400 ? `${Math.floor(s / 3600)}h ago` : `${Math.floor(s / 86400)}d ago`;
}

// --- a node, as a row ---------------------------------------------------------
// Hovering lights the node's path back to the provider on the canvas; clicking
// selects it and flies the camera there.
export function NodeRow({ node, status, meta, trail = true }: { node: GraphNode; status?: Status; meta?: ReactNode; trail?: boolean }) {
  const setFocus = useStore((s) => s.setFocus);
  const select = useStore((s) => s.select);
  const selected = useStore((s) => s.selectedId === node.id);
  const color = status ? STATUS_COLOR[status] : node.kind === "provider" ? "var(--mint)" : communityColor(node.community);
  return (
    <button
      className={`node-row${selected ? " is-selected" : ""}`}
      onMouseEnter={() => {
        const view = useStore.getState().view;
        setFocus(trail && view.hits.has(node.id) ? pathToProvider(view, node.id) : [node.id]);
      }}
      onMouseLeave={() => setFocus(null)}
      onClick={() => select(node.id, { fly: true })}
    >
      <span className={`dot kind-${node.kind}`} style={{ "--c": color } as React.CSSProperties} />
      <span className="node-row-label" title={node.label}>{shortLabel(node.label)}</span>
      {meta !== undefined && <span className="node-row-meta">{meta}</span>}
    </button>
  );
}

/** Hover target that lights up a set of graph nodes. */
export function focusProps(ids: string[]) {
  return {
    onMouseEnter: () => useStore.getState().setFocus(ids),
    onMouseLeave: () => useStore.getState().setFocus(null),
  };
}
