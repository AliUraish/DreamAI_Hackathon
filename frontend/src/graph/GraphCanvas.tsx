import { useEffect, useMemo, useRef } from "react";
import { useStore } from "../lib/store";
import type { View } from "../lib/derive";
import type { TrafficSnapshot } from "../lib/types";
import { createLayout, DOC_ID, PR_ID, type Layout, type SimNode } from "./layout";
import { computeStatuses, draw, type Camera, type Status } from "./render";
import type { SimLink } from "./layout";

const MIN_K = 0.25;
const MAX_K = 4;

/** Screen space the floating panels cover, so "fit" centres on what is actually visible. */
function insets(w: number, h: number) {
  if (w >= 1100) return { l: 292, r: 452, t: 76, b: 36 };
  if (w > 760) return { l: 28, r: 372, t: 76, b: 36 };
  return { l: 20, r: 20, t: 64, b: h * 0.42 };
}

/** The new nodes a review recommends, placed beside the node under pressure, with the links they would carry. */
type NewNode = { id: string; label: string; from: string; provider?: string };

/** What should be drawn as "could exist": the recommended option of a review on screen, otherwise whatever the live traffic suggests. */
function proposals(view: View, traffic: TrafficSnapshot | null): { nodeId: string; nodes: NewNode[]; badge?: string }[] {
  const review = view.review;
  const option = review?.options.find((o) => o.id === review.recommended);
  if (review && option) return [{ nodeId: review.nodeId, nodes: option.new_nodes }];
  if (view.started) return [];
  return (traffic?.suggestions ?? []).map((s) => ({ nodeId: s.nodeId, nodes: s.new_nodes, badge: `p95 −${s.gain.p95_pct}%` }));
}

function proposedLinks(layout: Layout, view: View, traffic: TrafficSnapshot | null): { links: { from: string; to: string }[]; ids: Set<string> } {
  const links: { from: string; to: string }[] = [];
  const ids = new Set<string>();
  for (const p of proposals(view, traffic)) addProposal(layout, view, p.nodeId, p.nodes, p.badge, links, ids);
  return { links, ids };
}

function addProposal(layout: Layout, view: View, hotId: string, nodes: NewNode[], badge: string | undefined, links: { from: string; to: string }[], ids: Set<string>) {
  const hot = layout.byId.get(hotId);
  if (!hot) return;
  nodes.forEach((n, i) => {
    const from = layout.byId.get(n.from);
    const callsOut = [...(layout.neighbors.get(hotId) ?? [])].find((id) => id.startsWith("provider:"));
    const exit = layout.byId.get(n.provider ?? view.release?.providerId ?? callsOut ?? "");
    if (!from) return;
    ids.add(n.id);
    if (!layout.byId.has(n.id)) {
      // Halfway between the caller and where its calls leave the repo, pushed off the existing edge.
      const tx = exit?.x ?? hot.x, ty = exit?.y ?? hot.y;
      const mx = (from.x + tx) / 2, my = (from.y + ty) / 2;
      const d = Math.hypot(tx - from.x, ty - from.y) || 1;
      const side = i % 2 === 0 ? 1 : -1;
      layout.addSynthetic(n.id, badge ? `+ ${n.label} · ${badge}` : `+ ${n.label}`, "doc", mx + (-(ty - from.y) / d) * 46 * side, my + ((tx - from.x) / d) * 46 * side);
    }
    links.push({ from: n.from, to: n.id });
    if (exit) links.push({ from: n.id, to: exit.id });
  });
}

function ensureSynthetics(layout: Layout, view: View) {
  if (!view.release) return;
  const prov = layout.byId.get(view.release.providerId);
  if (!prov) return;
  const c = layout.centroid();
  if (view.docs && !layout.byId.has(DOC_ID)) {
    // Beside the provider, on the side facing away from the repo.
    const a = Math.atan2(prov.y - c.y, prov.x - c.x) + 0.95;
    layout.addSynthetic(DOC_ID, "migration guide", "doc", prov.x + Math.cos(a) * 92, prov.y + Math.sin(a) * 92);
  }
  if (view.pr && !layout.byId.has(PR_ID)) {
    const files = [...view.patches.keys()].map((id) => layout.byId.get(id)).filter((n): n is SimNode => !!n);
    if (!files.length) return;
    const fx = files.reduce((s, n) => s + n.x, 0) / files.length;
    const fy = files.reduce((s, n) => s + n.y, 0) / files.length;
    // Downstream of the path (provider -> files -> PR): past the farthest
    // changed file along that axis, nudged sideways off the traced edges.
    const d = Math.hypot(fx - prov.x, fy - prov.y) || 1;
    const ux = (fx - prov.x) / d, uy = (fy - prov.y) / d;
    const far = Math.max(...files.map((n) => (n.x - prov.x) * ux + (n.y - prov.y) * uy));
    layout.addSynthetic(PR_ID, `PR #${view.pr.number}`, "pr", prov.x + ux * (far + 120) + uy * 70, prov.y + uy * (far + 120) - ux * 70);
  }
}

export function GraphCanvas() {
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const graph = useStore((s) => s.graph);
  const layout = useMemo(() => createLayout(graph), [graph]);

  useEffect(() => {
    const wrap = wrapRef.current!;
    const canvas = canvasRef.current!;
    const ctx = canvas.getContext("2d")!;
    const cam: Camera = { x: 0, y: 0, k: 1 };
    const target: Camera = { x: 0, y: 0, k: 1 };
    let tau = 260;
    let w = 0, h = 0, dpr = 1;
    let sized = false;
    let refit = false;

    const frameFor = (ids: string[] | null, maxZoom = 1.5): Camera => {
      const ns = ids ? ids.map((id) => layout.byId.get(id)).filter((n): n is SimNode => !!n) : layout.nodes.filter((n) => !n.synthetic);
      if (!ns.length) return { ...target };
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const n of ns) {
        x0 = Math.min(x0, n.x); x1 = Math.max(x1, n.x);
        y0 = Math.min(y0, n.y); y1 = Math.max(y1, n.y);
      }
      const pad = insets(w, h);
      const aw = Math.max(120, w - pad.l - pad.r), ah = Math.max(120, h - pad.t - pad.b);
      const k = Math.max(MIN_K, Math.min(maxZoom, Math.min(aw / (x1 - x0 + 150), ah / (y1 - y0 + 150))));
      return { k, x: pad.l + aw / 2 - ((x0 + x1) / 2) * k, y: pad.t + ah / 2 - ((y0 + y1) / 2) * k };
    };
    const flyTo = (c: Camera, t = 260) => {
      Object.assign(target, c);
      tau = t;
    };

    const resize = () => {
      const rect = wrap.getBoundingClientRect();
      w = rect.width; h = rect.height;
      dpr = Math.min(2, window.devicePixelRatio || 1);
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      refit = true;
      if (!sized && w > 0) {
        sized = true;
        Object.assign(cam, frameFor(null, 1.2));
        Object.assign(target, cam);
      }
    };
    const ro = new ResizeObserver(resize);
    ro.observe(wrap);
    resize();

    // --- pointer interaction ------------------------------------------------
    const world = (px: number, py: number) => ({ x: (px - cam.x) / cam.k, y: (py - cam.y) / cam.k });
    const pick = (px: number, py: number): SimNode | null => {
      const { view, hiddenCommunities } = useStore.getState();
      const p = world(px, py);
      let best: SimNode | null = null;
      let bestD = Infinity;
      const now = performance.now();
      for (const n of layout.nodes) {
        if (n.id.startsWith("proposed:")) continue; // a proposal is explained in the panel, not inspected as code
        if (n.id === DOC_ID ? !view.docs : n.id === PR_ID ? !view.pr : hiddenCommunities.has(n.community) || now < n.bornAt) continue;
        const d = Math.hypot(n.dx - p.x, n.dy - p.y);
        const reach = Math.max(n.r + 3, 9 / cam.k);
        if (d < reach && d < bestD) (best = n), (bestD = d);
      }
      return best;
    };

    /** The connection under the pointer: nearest segment within a few pixels. */
    const pickLink = (px: number, py: number): SimLink | null => {
      const { hiddenCommunities } = useStore.getState();
      const p = world(px, py);
      const now = performance.now();
      const reach = 6 / cam.k;
      let best: SimLink | null = null;
      let bestD = reach;
      for (const l of layout.links) {
        const a = l.source, b = l.target;
        if (now < a.bornAt || now < b.bornAt || hiddenCommunities.has(a.community) || hiddenCommunities.has(b.community)) continue;
        const vx = b.dx - a.dx, vy = b.dy - a.dy;
        const t = Math.max(0, Math.min(1, ((p.x - a.dx) * vx + (p.y - a.dy) * vy) / (vx * vx + vy * vy || 1)));
        const d = Math.hypot(a.dx + vx * t - p.x, a.dy + vy * t - p.y);
        if (d < bestD) (best = l), (bestD = d);
      }
      return best;
    };
    let hoverLink: SimLink | null = null;

    let drag: { node: SimNode | null; link: SimLink | null; sx: number; sy: number; cx: number; cy: number; moved: boolean } | null = null;
    const local = (e: PointerEvent | WheelEvent | MouseEvent) => {
      const r = canvas.getBoundingClientRect();
      return { px: e.clientX - r.left, py: e.clientY - r.top };
    };
    const takeOver = () => {
      if (useStore.getState().follow) useStore.getState().setFollow(false);
    };

    const onDown = (e: PointerEvent) => {
      const { px, py } = local(e);
      canvas.setPointerCapture(e.pointerId);
      const node = pick(px, py);
      drag = { node, link: node ? null : pickLink(px, py), sx: px, sy: py, cx: target.x, cy: target.y, moved: false };
    };
    const onMove = (e: PointerEvent) => {
      const { px, py } = local(e);
      if (!drag) {
        const n = pick(px, py);
        const l = n ? null : pickLink(px, py);
        useStore.getState().setHover(n?.id ?? null);
        if (l !== hoverLink) {
          hoverLink = l;
          useStore.getState().setFocus(l ? [l.source.id, l.target.id] : null);
        }
        canvas.style.cursor = n || l ? "pointer" : "grab";
        return;
      }
      if (!drag.moved && Math.hypot(px - drag.sx, py - drag.sy) < 4) return;
      if (!drag.moved) {
        drag.moved = true;
        if (drag.node) layout.sim.alphaTarget(0.22).restart();
        else takeOver();
      }
      if (drag.node) {
        const p = world(px, py);
        drag.node.fx = p.x;
        drag.node.fy = p.y;
      } else {
        target.x = cam.x = drag.cx + (px - drag.sx);
        target.y = cam.y = drag.cy + (py - drag.sy);
        canvas.style.cursor = "grabbing";
      }
    };
    const onUp = (e: PointerEvent) => {
      if (!drag) return;
      const d = drag;
      drag = null;
      canvas.releasePointerCapture(e.pointerId);
      if (d.node && d.moved) {
        layout.sim.alphaTarget(0);
        if (!d.node.synthetic) d.node.fx = d.node.fy = null;
      } else if (!d.moved) {
        if (d.link) useStore.getState().selectLink({ source: d.link.source.id, target: d.link.target.id });
        else useStore.getState().select(d.node?.id ?? null);
      }
    };
    const onLeave = () => {
      useStore.getState().setHover(null);
      if (hoverLink) useStore.getState().setFocus(null);
      hoverLink = null;
    };
    const onDbl = (e: MouseEvent) => {
      const { px, py } = local(e);
      const n = pick(px, py);
      if (n) useStore.getState().flyTo([n.id, ...(layout.neighbors.get(n.id) ?? [])], 2);
    };
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      takeOver();
      const { px, py } = local(e);
      // Wheel and pinch (ctrl+wheel) zoom about the cursor; a sideways swipe pans.
      if (!e.ctrlKey && Math.abs(e.deltaX) > Math.abs(e.deltaY) * 0.6 && e.deltaMode === 0) {
        target.x -= e.deltaX;
        target.y -= e.deltaY;
        tau = 60;
        return;
      }
      const k = Math.max(MIN_K, Math.min(MAX_K, target.k * Math.exp(-e.deltaY * (e.ctrlKey ? 0.012 : 0.0022))));
      const wx = (px - target.x) / target.k, wy = (py - target.y) / target.k;
      flyTo({ k, x: px - wx * k, y: py - wy * k }, 70);
    };
    canvas.addEventListener("pointerdown", onDown);
    canvas.addEventListener("pointermove", onMove);
    canvas.addEventListener("pointerup", onUp);
    canvas.addEventListener("pointerleave", onLeave);
    canvas.addEventListener("dblclick", onDbl);
    canvas.addEventListener("wheel", onWheel, { passive: false });

    // --- frame loop -----------------------------------------------------------
    let raf = 0;
    let last = performance.now();
    let lastView: View | null = null;
    let statuses = new Map<string, Status>();
    let proposed: { from: string; to: string }[] = [];
    let proposedIds = new Set<string>();
    let lastSuggested: unknown = null;
    let lastCmd = -1;
    let directedFor: View | null = null;
    let wasFollowing = false;

    const frame = (now: number) => {
      const dt = Math.min(64, now - last);
      last = now;
      const store = useStore.getState();
      store.advance(dt);
      const s = useStore.getState();

      if (s.view !== lastView || s.traffic?.suggestions !== lastSuggested) {
        lastSuggested = s.traffic?.suggestions;
        ({ links: proposed, ids: proposedIds } = proposedLinks(layout, s.view, s.traffic));
      }
      if (s.view !== lastView) {
        lastView = s.view;
        ensureSynthetics(layout, s.view);
        layout.setSpotlight(s.view.hits.keys());
        statuses = computeStatuses(s.view, layout.byId);
      }

      // Explicit camera requests from the UI.
      if (s.cameraCmd && s.cameraCmd.nonce !== lastCmd) {
        const cmd = s.cameraCmd;
        lastCmd = cmd.nonce;
        if (cmd.type === "fit") flyTo(frameFor(null, 1.2), 300);
        else if (cmd.type === "nodes") flyTo(frameFor(cmd.ids, cmd.maxZoom ?? 1.6), 300);
        else {
          const pad = insets(w, h);
          const cx = pad.l + (w - pad.l - pad.r) / 2, cy = pad.t + (h - pad.t - pad.b) / 2;
          const k = Math.max(MIN_K, Math.min(MAX_K, target.k * cmd.factor));
          flyTo({ k, x: cx - ((cx - target.x) / target.k) * k, y: cy - ((cy - target.y) / target.k) * k }, 140);
        }
      }

      // The director: while following, keep whatever the pipeline is touching in frame.
      if (s.follow && sized && (directedFor !== s.view || !wasFollowing || refit)) {
        directedFor = s.view;
        refit = false;
        const v = s.view;
        if (!v.release) flyTo(frameFor(null, 1.2), 420);
        else if (v.hits.size === 0) flyTo(frameFor([v.release.providerId, ...(layout.neighbors.get(v.release.providerId) ?? [])], 1.7), 480);
        else {
          const ids = [v.release.providerId, ...v.hits.keys()];
          if (v.docs) ids.push(DOC_ID);
          if (v.pr) ids.push(PR_ID);
          flyTo(frameFor(ids, 1.55), 520);
        }
      }
      wasFollowing = s.follow;

      const f = 1 - Math.exp(-dt / tau);
      cam.x += (target.x - cam.x) * f;
      cam.y += (target.y - cam.y) * f;
      cam.k += (target.k - cam.k) * f;

      if (w > 0)
        draw({
          ctx, w, h, dpr, cam, now, clock: s.clock, layout, view: s.view, statuses,
          hoverId: s.hoverId, selectedId: s.selectedId,
          focus: s.focusIds ? new Set(s.focusIds) : null,
          hidden: s.hiddenCommunities, base: s.nodeStatus, selectedLink: s.selectedLink, traffic: s.traffic, proposed, proposedIds, fast: s.sim.running,
        });
      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);

    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      layout.sim.stop();
      canvas.removeEventListener("pointerdown", onDown);
      canvas.removeEventListener("pointermove", onMove);
      canvas.removeEventListener("pointerup", onUp);
      canvas.removeEventListener("pointerleave", onLeave);
      canvas.removeEventListener("dblclick", onDbl);
      canvas.removeEventListener("wheel", onWheel);
    };
  }, [layout]);

  return (
    <div ref={wrapRef} className="graph">
      <canvas ref={canvasRef} aria-label="Code graph of the connected repository" />
    </div>
  );
}
