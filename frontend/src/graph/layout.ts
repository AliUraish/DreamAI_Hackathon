import {
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  forceX,
  forceY,
  type Simulation,
} from "d3-force";
import type { GraphData, GraphNode } from "../lib/types";

export interface SimNode extends GraphNode {
  x: number;
  y: number;
  vx?: number;
  vy?: number;
  fx?: number | null;
  fy?: number | null;
  r: number;
  degree: number;
  /** Per-node offset for the idle drift. */
  phase: number;
  /** Displayed position (x/y plus drift), refreshed every frame. */
  dx: number;
  dy: number;
  /** Wall time (performance.now) at which the node grows into the picture, from the neighbour that led to it. */
  bornAt: number;
  parent?: SimNode;
}

export interface SimLink {
  id: string;
  source: SimNode;
  target: SimNode;
  relation: string;
}

export const DOC_ID = "synthetic:migration-guide";
export const PR_ID = "synthetic:pull-request";

/** Providers sit outside the repo, this many ring-radii from the centre. */
const PROVIDER_REACH = 1.6;

export interface Layout {
  nodes: SimNode[];
  links: SimLink[];
  byId: Map<string, SimNode>;
  neighbors: Map<string, Set<string>>;
  relationOf(a: string, b: string): string | undefined;
  /** Nodes in the order they appear, parents before children. */
  growOrder: SimNode[];
  /** When the last node has finished appearing. */
  grownAt: number;
  /** Nodes the run has reached. They repel each other a little harder than the rest. */
  setSpotlight(ids: Iterable<string>): void;
  sim: Simulation<SimNode, SimLink>;
  /** Pin a synthetic node (migration guide, PR) into the scene; others make room for it. */
  addSynthetic(id: string, label: string, kind: "doc" | "pr", x: number, y: number): SimNode;
  centroid(): { x: number; y: number };
}

export function createLayout(graph: GraphData): Layout {
  const degree = new Map<string, number>();
  for (const l of graph.links) {
    degree.set(l.source, (degree.get(l.source) ?? 0) + 1);
    degree.set(l.target, (degree.get(l.target) ?? 0) + 1);
  }

  const communities = [...new Set(graph.nodes.map((n) => n.community))].sort((a, b) => a - b);
  const ring = 42 * Math.sqrt(graph.nodes.length);
  const anchor = (c: number, scale = 1) => {
    const a = (communities.indexOf(c) / communities.length) * Math.PI * 2 + Math.PI;
    return { x: Math.cos(a) * ring * scale, y: Math.sin(a) * ring * 0.78 * scale };
  };

  const nodes: SimNode[] = graph.nodes.map((n, i) => {
    const d = degree.get(n.id) ?? 0;
    const base = n.kind === "provider" ? 12 : n.kind === "file" ? 5 : n.kind === "test" ? 4.2 : 3.6;
    const a = anchor(n.community, n.kind === "provider" ? PROVIDER_REACH : 1);
    const j = i * 2.399963;
    return {
      ...n,
      x: a.x + Math.cos(j) * 40,
      y: a.y + Math.sin(j) * 40,
      r: n.kind === "provider" ? base : base + Math.min(d, 9) * 0.38,
      degree: d,
      phase: i * 1.318,
      dx: 0,
      dy: 0,
      bornAt: 0,
    };
  });
  const byId = new Map(nodes.map((n) => [n.id, n]));

  const links: SimLink[] = graph.links.map((l) => ({
    id: l.id,
    source: byId.get(l.source)!,
    target: byId.get(l.target)!,
    relation: l.relation,
  }));

  const neighbors = new Map<string, Set<string>>();
  const relations = new Map<string, string>();
  for (const l of links) {
    (neighbors.get(l.source.id) ?? neighbors.set(l.source.id, new Set()).get(l.source.id)!).add(l.target.id);
    (neighbors.get(l.target.id) ?? neighbors.set(l.target.id, new Set()).get(l.target.id)!).add(l.source.id);
    relations.set(`${l.source.id}|${l.target.id}`, l.relation);
  }

  const sim = forceSimulation<SimNode, SimLink>(nodes)
    .force(
      "link",
      forceLink<SimNode, SimLink>(links)
        .distance((l) => (l.relation === "calls_api" ? 96 : l.relation === "contains" ? 24 : 44))
        .strength((l) => (l.relation === "contains" ? 0.9 : l.source.community === l.target.community ? 0.35 : 0.05)),
    )
    .force("charge", forceManyBody<SimNode>().strength((n) => (n.kind === "provider" ? -420 : -115)).distanceMax(420))
    .force("collide", forceCollide<SimNode>((n) => n.r + 5))
    .force("x", forceX<SimNode>((n) => anchor(n.community, n.kind === "provider" ? PROVIDER_REACH : 1).x).strength((n) => (n.kind === "provider" ? 0.5 : 0.13)))
    .force("y", forceY<SimNode>((n) => anchor(n.community, n.kind === "provider" ? PROVIDER_REACH : 1).y).strength((n) => (n.kind === "provider" ? 0.5 : 0.13)))
    .stop();

  // Nodes the pipeline has lit push each other apart, so a traced cluster
  // unfolds enough for its labels to be read.
  let spotlight: SimNode[] = [];
  sim.force("spotlight", (alpha: number) => {
    const reach = 92;
    for (let i = 0; i < spotlight.length; i++)
      for (let j = i + 1; j < spotlight.length; j++) {
        const a = spotlight[i], b = spotlight[j];
        const dx = b.x - a.x || 0.01, dy = b.y - a.y || 0.01;
        const d = Math.hypot(dx, dy);
        if (d >= reach) continue;
        const push = ((reach - d) / d) * alpha * 0.7;
        a.vx! -= dx * push; a.vy! -= dy * push;
        b.vx! += dx * push; b.vy! += dy * push;
      }
  });

  // Settle before first paint so every node already knows where it is going.
  for (let i = 0; i < 340; i++) sim.tick();
  sim.alphaDecay(0.035).velocityDecay(0.45);

  // The graph is not shown at once: it grows the way the context was built. Providers first, then the
  // functions that call them, then outwards one hop at a time, each node leaving from the one that led to it.
  const growOrder = [...nodes].sort((a, b) => (a.order ?? 1) - (b.order ?? 1));
  const step = Math.max(45, Math.min(150, 3200 / Math.max(1, nodes.length)));
  let t = performance.now() + 450;
  let wave = -1;
  for (const n of growOrder) {
    if ((n.order ?? 1) !== wave) {
      wave = n.order ?? 1;
      t += 380; // a breath between hops
    }
    n.bornAt = t;
    t += step;
    const order = n.order ?? 1;
    n.parent = [...(neighbors.get(n.id) ?? [])].map((id) => byId.get(id)!).filter((m) => (m.order ?? 1) < order)
      .sort((a, b) => (b.order ?? 1) - (a.order ?? 1))[0];
  }
  const grownAt = t + 700;

  return {
    nodes,
    links,
    byId,
    neighbors,
    growOrder,
    grownAt,
    sim,
    relationOf: (a, b) => relations.get(`${a}|${b}`) ?? relations.get(`${b}|${a}`),
    setSpotlight(ids) {
      const next = [...ids].map((id) => byId.get(id)).filter((n): n is SimNode => !!n && n.kind !== "provider");
      if (next.length === spotlight.length && next.every((n, i) => n === spotlight[i])) return;
      const grew = next.length > spotlight.length;
      spotlight = next;
      if (grew) sim.alpha(Math.max(sim.alpha(), 0.22)).restart();
    },
    addSynthetic(id, label, kind, x, y) {
      const existing = byId.get(id);
      if (existing) return existing;
      const n: SimNode = {
        id, label, kind, community: -1, communityName: "chowkidaar", synthetic: true,
        x, y, fx: x, fy: y, r: kind === "pr" ? 9 : 7, degree: 0, phase: 0, dx: x, dy: y, bornAt: 0,
      };
      nodes.push(n);
      growOrder.push(n);
      byId.set(id, n);
      sim.nodes(nodes).alpha(0.25).restart();
      return n;
    },
    centroid() {
      let x = 0, y = 0, c = 0;
      for (const n of nodes) if (!n.synthetic) (x += n.x), (y += n.y), c++;
      return { x: x / Math.max(1, c), y: y / Math.max(1, c) };
    },
  };
}
