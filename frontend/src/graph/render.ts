import type { View } from "../lib/derive";
import type { BaseStatus, TrafficSnapshot } from "../lib/types";
import { DOC_ID, PR_ID, type Layout, type SimNode } from "./layout";

export const C = {
  mint: "#6ee7b0",
  coral: "#ff6b5e",
  amber: "#ffb454",
  blue: "#7aa8ff",
  violet: "#b692ff",
  slate: "#8896a6",
  text: "#e6ece8",
};

// Muted categorical set for communities. No reds: red is reserved for "breaking".
export const COMMUNITY_COLORS = ["#5b8fd6", "#76b7b2", "#d98a3d", "#b07aa1", "#a9a39b", "#59a14f", "#d9c04a", "#9c755f", "#9ecae8"];
export const communityColor = (c: number) => (c < 0 ? "#ffffff" : COMMUNITY_COLORS[c % COMMUNITY_COLORS.length]);

export type Status = "breaking" | "risk" | "patching" | "ok" | "pending" | "fail";
export const STATUS_COLOR: Record<Status, string> = {
  breaking: C.coral,
  fail: C.coral,
  risk: C.amber,
  patching: C.amber,
  ok: C.mint,
  pending: C.slate,
};

/** Where each traced node stands in the migration. Recomputed only when the view changes. */
export function computeStatuses(view: View, byId: Map<string, { sourceFile?: string }>): Map<string, Status> {
  const out = new Map<string, Status>();
  if (!view.release) return out;

  // A patch event with no hunks means nothing was generated for that file.
  const written = (id: string) => {
    const p = view.patches.get(id);
    return p?.status === "done" && (p.patch?.hunks.length ?? 0) > 0;
  };
  // Symbols turn green with the file that hosts them. Patches are keyed by node
  // id, symbols point at their host by path, so match on either.
  const writtenPaths = new Set<string>();
  for (const [id, p] of view.patches) if (written(id)) writtenPaths.add(id).add(p.patch!.path);
  const changeIds = [...view.hits.values()].filter((h) => h.role === "change").map((h) => h.nodeId);
  const allPatched = changeIds.length > 0 && changeIds.every(written);
  const patched = view.checks.filter((c) => c.phase === "patched");
  const verified =
    !!view.pr || (patched.length > 0 && patched.every((c) => c.status === "passed"));

  out.set(view.release.providerId, verified ? "ok" : "breaking");
  for (const hit of view.hits.values()) {
    const node = byId.get(hit.nodeId);
    if (hit.role === "change") {
      const p = view.patches.get(hit.nodeId);
      out.set(hit.nodeId, written(hit.nodeId) ? "ok" : p?.status === "patching" ? "patching" : "breaking");
    } else if (hit.role === "symbol") {
      out.set(hit.nodeId, (node?.sourceFile && writtenPaths.has(node.sourceFile)) || allPatched ? "ok" : "breaking");
    } else if (hit.role === "dependent") {
      out.set(hit.nodeId, verified ? "ok" : "risk");
    } else {
      const mine = view.checks.filter((c) => c.nodeId === hit.nodeId);
      const after = mine.filter((c) => c.phase === "patched");
      if (after.length && after.every((c) => c.status === "passed")) out.set(hit.nodeId, "ok");
      else if (mine.some((c) => c.phase === "baseline" && c.status === "failed")) out.set(hit.nodeId, "fail");
      else out.set(hit.nodeId, "pending");
    }
  }
  return out;
}

export interface Camera {
  x: number;
  y: number;
  k: number;
}

export interface Scene {
  ctx: CanvasRenderingContext2D;
  w: number;
  h: number;
  dpr: number;
  cam: Camera;
  /** Wall time, for ambient motion that keeps going while the run is paused. */
  now: number;
  /** Run time, for everything that must replay identically when scrubbing. */
  clock: number;
  layout: Layout;
  view: View;
  statuses: Map<string, Status>;
  hoverId: string | null;
  selectedId: string | null;
  focus: Set<string> | null;
  hidden: Set<number>;
  /** What the backend reports per node when no run has lit it. */
  base: Map<string, BaseStatus>;
  selectedLink: { source: string; target: string } | null;
  /** Calls per second per node and link; drawn as moving dots and load rings. */
  traffic: TrafficSnapshot | null;
  /** Links a review proposes (caller -> new node -> provider), drawn dashed with their own traffic. */
  proposed: { from: string; to: string }[];
  /** Proposed nodes that should be on screen right now (they are created once and hidden when no longer proposed). */
  proposedIds: Set<string>;
  /** The one-button simulation is playing: traffic dots move fast. Otherwise they move slowly, all at one speed. */
  fast?: boolean;
}

/** Graph units per millisecond for a traffic dot, and how much faster time runs while a simulation plays. */
const FLOW_SPEED = 0.03, FLOW_FAST = 4;
let flowClock = 0, flowSeen = 0;

const GROW_MS = 720;
/** 0 before a node is born, easing to 1 as it arrives. */
export const grown = (n: SimNode, now: number) => (n.bornAt <= 0 ? 1 : easeOut((now - n.bornAt) / GROW_MS));

const MONO = '"JetBrains Mono Variable", ui-monospace, monospace';
const clamp01 = (x: number) => (x < 0 ? 0 : x > 1 ? 1 : x);
const easeOut = (x: number) => 1 - Math.pow(1 - clamp01(x), 3);
const frac = (x: number) => x - Math.floor(x);

function rgba(hex: string, a: number): string {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

function hash(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619);
  return ((h >>> 0) % 1000) / 1000;
}

const sprites = new Map<string, HTMLCanvasElement>();
function glow(ctx: CanvasRenderingContext2D, color: string, x: number, y: number, radius: number, alpha: number) {
  let sprite = sprites.get(color);
  if (!sprite) {
    sprite = document.createElement("canvas");
    sprite.width = sprite.height = 128;
    const g = sprite.getContext("2d")!;
    const grad = g.createRadialGradient(64, 64, 0, 64, 64, 64);
    grad.addColorStop(0, rgba(color, 0.55));
    grad.addColorStop(0.35, rgba(color, 0.16));
    grad.addColorStop(1, rgba(color, 0));
    g.fillStyle = grad;
    g.fillRect(0, 0, 128, 128);
    sprites.set(color, sprite);
  }
  ctx.globalAlpha = alpha;
  ctx.globalCompositeOperation = "lighter";
  ctx.drawImage(sprite, x - radius, y - radius, radius * 2, radius * 2);
  ctx.globalCompositeOperation = "source-over";
  ctx.globalAlpha = 1;
}

export function shortLabel(label: string): string {
  const parts = label.split("/");
  return parts.length > 2 ? parts.slice(-2).join("/") : label;
}

function hexPath(ctx: CanvasRenderingContext2D, x: number, y: number, r: number) {
  ctx.beginPath();
  for (let i = 0; i < 6; i++) {
    const a = (Math.PI / 3) * i - Math.PI / 2;
    const px = x + Math.cos(a) * r;
    const py = y + Math.sin(a) * r;
    if (i === 0) ctx.moveTo(px, py);
    else ctx.lineTo(px, py);
  }
  ctx.closePath();
}

interface Label {
  x: number;
  y: number;
  r: number;
  text: string;
  sub?: string;
  color: string;
  alpha: number;
  priority: number;
  above: boolean;
  italic?: boolean;
}

export function draw(s: Scene) {
  const { ctx, w, h, dpr, cam, now, clock, layout, view, statuses, hidden } = s;
  const { k } = cam;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, w, h);

  const sx = (n: SimNode) => n.dx * k + cam.x;
  const sy = (n: SimNode) => n.dy * k + cam.y;
  const nodeScale = Math.max(0.6, Math.pow(k, 0.8));

  const isVisible = (n: SimNode) => {
    if (n.id === DOC_ID) return !!view.docs;
    if (n.id === PR_ID) return !!view.pr;
    if (n.id.startsWith("proposed:")) return s.proposedIds.has(n.id);
    return now >= n.bornAt && !hidden.has(n.community);
  };

  // Parents first: a node that is still arriving travels out from where its parent is right now.
  for (const n of layout.growOrder) {
    const drift = n.synthetic ? 0 : 1.6;
    const tx = n.x + Math.sin(now / 2400 + n.phase) * drift;
    const ty = n.y + Math.cos(now / 2900 + n.phase * 1.7) * drift;
    const g = grown(n, now);
    if (g < 1 && n.parent) {
      n.dx = n.parent.dx + (tx - n.parent.dx) * g;
      n.dy = n.parent.dy + (ty - n.parent.dy) * g;
    } else {
      n.dx = tx;
      n.dy = ty;
    }
  }

  // --- who is lit -----------------------------------------------------------
  // `lit` holds 0..1 "pop" progress for every node the pipeline has reached.
  const lit = new Map<string, number>();
  if (view.release) lit.set(view.release.providerId, clamp01((clock - view.release.at) / 300));
  for (const hit of view.hits.values()) {
    const q = clamp01((clock - hit.at - 300) / 380);
    if (q > 0) lit.set(hit.nodeId, q);
  }
  if (view.docs) lit.set(DOC_ID, clamp01((clock - view.docs.at) / 400));
  for (const id of s.proposedIds) lit.set(id, view.review ? clamp01((clock - view.review.at) / 500) : 1);
  if (view.pr) lit.set(PR_ID, clamp01((clock - view.pr.at) / 400));

  const attention = new Set<string>();
  if (s.hoverId) {
    attention.add(s.hoverId);
    for (const id of layout.neighbors.get(s.hoverId) ?? []) attention.add(id);
  }
  if (s.focus) for (const id of s.focus) attention.add(id);
  const hasAttention = attention.size > 0;

  const traceAt = view.stageStart.trace;
  const dimT = traceAt === undefined ? 0 : easeOut((clock - traceAt) / 900);
  const restAlpha = !view.release ? 0.88 : 0.5 - dimT * 0.33;
  const restLink = !view.release ? 0.13 : 0.085 - dimT * 0.04;

  // --- resting links --------------------------------------------------------
  ctx.lineWidth = 1;
  for (const l of layout.links) {
    if (!isVisible(l.source) || !isVisible(l.target)) continue;
    const near = hasAttention && attention.has(l.source.id) && attention.has(l.target.id) &&
      (s.hoverId === l.source.id || s.hoverId === l.target.id || !s.hoverId);
    ctx.strokeStyle = near ? "rgba(255,255,255,0.42)" : `rgba(255,255,255,${hasAttention ? restLink * 0.6 : restLink})`;
    ctx.beginPath();
    ctx.moveTo(sx(l.source), sy(l.source));
    ctx.lineTo(sx(l.target), sy(l.target));
    ctx.stroke();
  }

  if (s.selectedLink) {
    const a = layout.byId.get(s.selectedLink.source), b = layout.byId.get(s.selectedLink.target);
    if (a && b && isVisible(a) && isVisible(b)) {
      for (const [lw, al] of [[8, 0.08], [3.5, 0.22], [1.6, 0.95]]) {
        ctx.lineWidth = lw;
        ctx.strokeStyle = `rgba(255,255,255,${al})`;
        ctx.beginPath();
        ctx.moveTo(sx(a), sy(a));
        ctx.lineTo(sx(b), sy(b));
        ctx.stroke();
      }
      ctx.lineWidth = 1;
    }
  }

  // --- traffic: dots move caller -> callee, more of them the busier the link, slower the longer a call takes ----------
  const traffic = s.traffic;
  // Every dot moves at the same slow speed, whatever the link; how busy a link is shows in how many dots it carries.
  // While the one-button simulation plays, time runs faster for all of them. A running clock, so a change of pace never jumps.
  flowClock += Math.min(100, Math.max(0, now - flowSeen)) * (s.fast ? FLOW_FAST : 1);
  flowSeen = now;
  const flow = (a: SimNode, b: SimNode, rps: number, _meanMs: number, color: string, seed: number, dim = 1) => {
    const count = Math.max(1, Math.min(9, Math.round(rps * 1.6)));
    const period = Math.max(1200, Math.hypot(b.x - a.x, b.y - a.y) / FLOW_SPEED); // ms for one dot to cross the link
    const x1 = sx(a), y1 = sy(a), x2 = sx(b), y2 = sy(b);
    for (let i = 0; i < count; i++) {
      const t = frac(flowClock / period + seed + i / count);
      ctx.fillStyle = rgba(color, 0.9 * Math.sin(t * Math.PI) * dim);
      ctx.beginPath();
      ctx.arc(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, 1.2 + Math.min(1.6, rps * 0.18), 0, Math.PI * 2);
      ctx.fill();
    }
  };
  for (const l of layout.links) {
    if (l.relation === "contains" || !isVisible(l.source) || !isVisible(l.target)) continue;
    const st = statuses.get(l.target.id);
    if (!traffic) {
      // No traffic is reported or simulated: one slow dot per API call site, so the picture still says which way calls go.
      if (l.relation === "calls_api") flow(l.source, l.target, 0.4, 2600, st === "breaking" ? C.coral : C.mint, hash(l.id), view.release && !st ? 0.35 : 1);
      continue;
    }
    const t = traffic.links[`${l.source.id}->${l.target.id}`];
    if (!t || t.rps <= 0) continue;
    const load = traffic.nodes[l.target.id]?.load ?? 0;
    flow(l.source, l.target, t.rps, t.mean_ms, load >= 0.85 ? C.coral : load >= 0.6 ? C.amber : C.mint, hash(l.id));
  }
  // What the review proposes: dashed links through the new node, already carrying their share.
  for (const p of s.proposed) {
    const a = layout.byId.get(p.from), b = layout.byId.get(p.to);
    if (!a || !b || !isVisible(a) || !isVisible(b)) continue;
    ctx.setLineDash([4, 5]);
    ctx.lineWidth = 1.3;
    ctx.strokeStyle = rgba(C.mint, 0.75);
    ctx.beginPath();
    ctx.moveTo(sx(a), sy(a));
    ctx.lineTo(sx(b), sy(b));
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.lineWidth = 1;
    flow(a, b, 2.4, 900, C.mint, hash(p.from + p.to));
  }

  const labels: Label[] = [];

  // --- hot links: the traced path, doc beams, PR edges ------------------------
  const hotLine = (a: SimNode, b: SimNode, p: number, color: string, dashed = false) => {
    const x1 = sx(a), y1 = sy(a);
    const x2 = x1 + (sx(b) - x1) * p, y2 = y1 + (sy(b) - y1) * p;
    if (dashed) ctx.setLineDash([5, 5]);
    for (const [lw, al] of dashed ? [[1.2, 0.7]] : [[7, 0.07], [3.5, 0.18], [1.7, 0.95]]) {
      ctx.lineWidth = lw;
      ctx.strokeStyle = rgba(color, al);
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.stroke();
    }
    ctx.setLineDash([]);
  };

  let hitIndex = 0;
  for (const hit of view.hits.values()) {
    const a = layout.byId.get(hit.from);
    const b = layout.byId.get(hit.nodeId);
    hitIndex++;
    if (!a || !b || !isVisible(a) || !isVisible(b)) continue;
    const p = easeOut((clock - hit.at) / 420);
    if (p <= 0) continue;
    const color = lit.has(hit.nodeId) ? STATUS_COLOR[statuses.get(hit.nodeId) ?? "breaking"] : C.coral;
    const dimmed = hasAttention && !(attention.has(a.id) && attention.has(b.id));
    ctx.globalAlpha = dimmed ? 0.3 : 1;
    hotLine(a, b, p, color);
    if (p >= 1) {
      const t = frac(now / 1700 + hitIndex * 0.37);
      ctx.fillStyle = rgba("#ffffff", 0.9 * Math.sin(t * Math.PI));
      ctx.beginPath();
      ctx.arc(sx(a) + (sx(b) - sx(a)) * t, sy(a) + (sy(b) - sy(a)) * t, 1.6, 0, Math.PI * 2);
      ctx.fill();
      if (k >= 0.85 && !dimmed) {
        const rel = layout.relationOf(a.id, b.id);
        const len = Math.hypot(sx(b) - sx(a), sy(b) - sy(a));
        if (rel && len > 70)
          labels.push({
            x: (sx(a) + sx(b)) / 2, y: (sy(a) + sy(b)) / 2, r: 2,
            text: rel.replace(/_/g, " "), color, alpha: 0.85, priority: 1, above: true, italic: true,
          });
      }
    }
    ctx.globalAlpha = 1;
  }

  const doc = layout.byId.get(DOC_ID);
  if (view.docs && doc && view.release) {
    const prov = layout.byId.get(view.release.providerId);
    if (prov) hotLine(prov, doc, easeOut((clock - view.docs.at) / 500), C.blue);
    const beamed = new Set<string>();
    for (const ex of view.excerpts)
      for (const id of ex.nodeIds) {
        if (beamed.has(id)) continue;
        beamed.add(id);
        const target = layout.byId.get(id);
        if (target && isVisible(target)) hotLine(doc, target, easeOut((clock - ex.at) / 600), C.blue, true);
      }
  }

  const pr = layout.byId.get(PR_ID);
  if (view.pr && pr) {
    let i = 0;
    for (const [id, p] of view.patches) {
      const from = layout.byId.get(id);
      if (from && p.status === "done") hotLine(from, pr, easeOut((clock - view.pr.at - i * 140) / 520), C.violet);
      i++;
    }
  }

  // --- shockwaves -----------------------------------------------------------
  const ripple = (n: SimNode | undefined, age: number, color: string, size: number, rings: number) => {
    if (!n || age < 0) return;
    for (let i = 0; i < rings; i++) {
      const a = (age - i * 360) / 2100;
      if (a <= 0 || a >= 1) continue;
      ctx.strokeStyle = rgba(color, (1 - a) * 0.55);
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(sx(n), sy(n), easeOut(a) * size * k + n.r, 0, Math.PI * 2);
      ctx.stroke();
    }
  };
  if (view.release) ripple(layout.byId.get(view.release.providerId), clock - view.release.at, C.coral, 300, 3);
  for (const [id, p] of view.patches) if (p.doneAt !== undefined) ripple(layout.byId.get(id), clock - p.doneAt, C.mint, 46, 1);
  if (view.pr) ripple(pr, clock - view.pr.at, C.violet, 170, 2);

  // --- nodes ----------------------------------------------------------------
  for (const n of layout.nodes) {
    if (!isVisible(n)) continue;
    const x = sx(n), y = sy(n);
    if (x < -60 || y < -60 || x > w + 60 || y > h + 60) continue;

    const q = lit.get(n.id);
    const isLit = q !== undefined;
    const status = statuses.get(n.id);
    const hit = view.hits.get(n.id);
    const inAttention = attention.has(n.id);
    const isHover = s.hoverId === n.id;
    const isSelected = s.selectedId === n.id;

    // With no run on screen, an open issue still shows: red at the API and its call sites, amber downstream.
    const base = isLit ? undefined : s.base.get(n.id);
    const flagged = base !== undefined && base !== "healthy";
    const inLink = s.selectedLink !== null && (s.selectedLink.source === n.id || s.selectedLink.target === n.id);

    let color = communityColor(n.community);
    if (n.id === DOC_ID) color = C.blue;
    else if (n.id === PR_ID) color = C.violet;
    else if (n.id.startsWith("proposed:")) color = C.mint;
    else if (isLit && status) color = STATUS_COLOR[status];
    else if (flagged) color = base === "breaking" ? C.coral : base === "affected" ? STATUS_COLOR.risk : C.mint;
    else if (n.kind === "provider") color = C.mint;

    const g = grown(n, now);
    let alpha = isLit || flagged || n.kind === "provider" ? 1 : restAlpha;
    if (hasAttention) alpha = inAttention ? 1 : alpha * (isLit ? 0.4 : 0.35);
    alpha *= Math.min(1, g * 1.6);

    const pop = isLit && q! < 1 ? 1 + Math.sin(q! * Math.PI) * 0.55 : 1;
    const arrive = g < 1 ? 0.25 + g * 0.75 + Math.sin(g * Math.PI) * 0.35 : 1; // swells a little as it lands
    const weight = hit?.role === "change" ? 1.25 : 1;
    const r = n.r * nodeScale * pop * weight * arrive;

    const t = traffic?.nodes[n.id];
    if (t && n.kind !== "provider" && g >= 1) {
      // A ring that fills with the node's load: the share of its concurrency budget that is busy.
      const load = Math.min(1, t.load);
      const ring = load >= 0.85 ? C.coral : load >= 0.6 ? C.amber : C.mint;
      const rr = r + 4 + (load >= 0.85 ? Math.sin(now / 160) * 1.4 : 0);
      if (load >= 0.85) glow(ctx, ring, x, y, rr * 4 + 10, 0.55 + Math.sin(now / 200) * 0.25);
      ctx.globalAlpha = Math.max(alpha, 0.85);
      ctx.lineWidth = 1.6;
      ctx.strokeStyle = rgba(ring, 0.22);
      ctx.beginPath(); ctx.arc(x, y, rr, 0, Math.PI * 2); ctx.stroke();
      ctx.strokeStyle = ring;
      ctx.beginPath(); ctx.arc(x, y, rr, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * Math.max(0.04, load)); ctx.stroke();
      ctx.lineWidth = 1;
      if (load >= 0.6 || s.hoverId === n.id) {
        ctx.globalAlpha = 1;
        ctx.font = `500 9.5px ${MONO}`;
        ctx.fillStyle = ring;
        ctx.textAlign = "center";
        ctx.fillText(`${t.rps.toFixed(1)}/s · p95 ${t.p95_ms >= 1000 ? (t.p95_ms / 1000).toFixed(1) + "s" : Math.round(t.p95_ms) + "ms"}`, x, y + rr + 12);
      }
    }
    if (g < 1 && g > 0) glow(ctx, color, x, y, r * 6 + 10, (1 - g) * 0.8);
    if (base === "breaking" && alpha > 0.5) glow(ctx, color, x, y, r * 4.6 + 8, 0.5 + Math.sin(now / 420) * 0.2);
    if ((isLit || isHover || isSelected || inLink) && alpha > 0.5) glow(ctx, color, x, y, r * (n.kind === "provider" ? 4.2 : 5.2) + 8, isLit ? 0.9 : 0.6);

    ctx.globalAlpha = alpha;
    if (n.kind === "provider") {
      hexPath(ctx, x, y, r + 3);
      ctx.fillStyle = "#000";
      ctx.fill();
      ctx.lineWidth = 1.6;
      ctx.strokeStyle = color;
      ctx.stroke();
      hexPath(ctx, x, y, r * 0.52);
      ctx.fillStyle = color;
      ctx.fill();
      if (status === "breaking") {
        ctx.strokeStyle = rgba(C.coral, 0.35 + Math.sin(now / 260) * 0.25);
        ctx.lineWidth = 1.2;
        hexPath(ctx, x, y, r + 8 + Math.sin(now / 260) * 1.5);
        ctx.stroke();
      }
    } else if (n.kind === "doc") {
      ctx.fillStyle = "#000";
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.6;
      ctx.beginPath();
      ctx.roundRect(x - r, y - r * 1.2, r * 2, r * 2.4, 2.5);
      ctx.fill();
      ctx.stroke();
      ctx.lineWidth = 1;
      for (const o of [-0.45, 0, 0.45]) {
        ctx.beginPath();
        ctx.moveTo(x - r * 0.5, y + o * r * 1.2);
        ctx.lineTo(x + r * 0.5, y + o * r * 1.2);
        ctx.stroke();
      }
    } else if (n.kind === "test") {
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.6;
      ctx.beginPath();
      ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.stroke();
      if (status === "ok" || status === "fail") {
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.arc(x, y, r * 0.45, 0, Math.PI * 2);
        ctx.fill();
      }
    } else {
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.fill();
      if (hit?.role === "change" && isLit) {
        ctx.strokeStyle = rgba(color, 0.55);
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        ctx.arc(x, y, r + 3.5, 0, Math.PI * 2);
        ctx.stroke();
      }
    }

    if (status === "patching") {
      const a0 = now / 180;
      ctx.strokeStyle = C.amber;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(x, y, r + 7, a0, a0 + Math.PI * 1.1);
      ctx.stroke();
    }
    if (s.focus?.has(n.id) || isHover) {
      ctx.strokeStyle = "rgba(255,255,255,0.9)";
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      ctx.arc(x, y, r + (n.kind === "provider" ? 9 : 5.5), 0, Math.PI * 2);
      ctx.stroke();
    }
    if (isSelected) {
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = 1.5;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.arc(x, y, r + (n.kind === "provider" ? 12 : 8.5), now / 1400, now / 1400 + Math.PI * 2);
      ctx.stroke();
      ctx.setLineDash([]);
    }
    ctx.globalAlpha = 1;

    // label candidates
    let priority = 0;
    if (isHover || isSelected) priority = 10;
    else if (n.kind === "provider") priority = 9;
    else if (s.focus?.has(n.id)) priority = 8;
    else if (isLit && q! > 0.5) priority = n.synthetic ? 7 : hit?.role === "change" ? 7 : hit?.role === "symbol" ? (k >= 1.15 ? 3 : 0) : 5;
    else if (inAttention) priority = 4;
    else if (k > 2.3) priority = 2;
    if (hasAttention && !inAttention && n.kind !== "provider") priority = Math.min(priority, isLit ? 3 : 0);
    if (priority > 0) {
      let above = false;
      if (n.kind !== "provider") {
        let vy = 0;
        for (const id of layout.neighbors.get(n.id) ?? []) if (lit.has(id) || attention.has(id)) vy += layout.byId.get(id)!.dy - n.dy;
        above = vy > 0;
      } else above = n.dy < layout.centroid().y;
      const released = view.release?.providerId === n.id ? view.release : undefined;
      labels.push({
        x, y, r: r + (n.kind === "provider" ? 8 : 4),
        text: n.kind === "provider" ? n.label : shortLabel(n.label),
        sub: n.kind === "provider" ? (released ? `${released.from} → ${released.to}` : n.version) : undefined,
        color: isLit || n.kind === "provider" ? (priority >= 5 ? "#f3f7f4" : color) : "#cfd6d1",
        alpha: priority >= 4 ? 1 : 0.75,
        priority,
        above,
      });
    }
  }

  // --- labels, highest priority first, skipping any that would collide ------
  labels.sort((a, b) => b.priority - a.priority);
  const placed: [number, number, number, number][] = [];
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.lineJoin = "round";
  for (const l of labels) {
    const size = l.italic ? 10 : l.priority >= 9 ? 12 : 11;
    ctx.font = `${l.italic ? "italic 500" : "650"} ${size}px ${MONO}`;
    const tw = ctx.measureText(l.text).width;
    const hgt = l.sub ? 28 : 14;
    const tryAt = (above: boolean): [number, number, number, number] => {
      const cy = l.italic ? l.y - 9 : above ? l.y - l.r - hgt / 2 - 3 : l.y + l.r + hgt / 2 + 3;
      return [l.x - tw / 2 - 3, cy - hgt / 2, tw + 6, hgt];
    };
    const free = (b: [number, number, number, number]) =>
      !placed.some((p) => b[0] < p[0] + p[2] && b[0] + b[2] > p[0] && b[1] < p[1] + p[3] && b[1] + b[3] > p[1]);
    let box = tryAt(l.above);
    if (!free(box)) {
      box = tryAt(!l.above);
      if (!free(box) && l.priority < 9) continue;
    }
    placed.push(box);
    const cx = box[0] + box[2] / 2;
    const top = box[1] + 7;
    ctx.globalAlpha = l.alpha;
    ctx.lineWidth = 3.5;
    ctx.strokeStyle = "rgba(0,0,0,0.9)";
    ctx.strokeText(l.text, cx, top);
    ctx.fillStyle = l.color;
    ctx.fillText(l.text, cx, top);
    if (l.sub) {
      ctx.font = `500 10px ${MONO}`;
      ctx.strokeText(l.sub, cx, top + 14);
      ctx.fillStyle = "rgba(230,236,232,0.6)";
      ctx.fillText(l.sub, cx, top + 14);
    }
    ctx.globalAlpha = 1;
  }
}
