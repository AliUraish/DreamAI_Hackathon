import { useEffect, useMemo, useRef, useState } from "react";
import { useStore } from "../lib/store";
import { communityColor, shortLabel } from "../graph/render";
import { Bell } from "./Notifications";
import { Icon } from "./shared";

export function TopBar() {
  const counts = useStore((s) =>
    `${s.graph.nodes.length} nodes · ${s.graph.links.length} edges` +
    (s.graph.repoNodesTotal ? ` · API pipeline slice of ${s.graph.repoNodesTotal}` : ""),
  );
  const connection = useStore((s) => s.connection);
  const system = useStore((s) => s.system);
  const hasProject = useStore((s) => s.projectId !== null);
  const setSearch = useStore((s) => s.setSearch);
  return (
    <header className="topbar">
      <div className="brand">
        <svg width="22" height="22" viewBox="0 0 32 32" aria-hidden>
          <path d="M5 23 L13 10 L20 20 L27 8" fill="none" stroke="var(--mint)" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />
          <circle cx="27" cy="8" r="3.2" fill="var(--mint)" />
        </svg>
        <span>CHOWKIDAAR</span>
      </div>
      {hasProject && <Projects />}
      {hasProject && <div className="crumb mono"><span className="muted">{counts}</span></div>}
      <div className="topbar-right">
        {system && (
          <span className="sys mono" title="Database · model · GitHub access">
            <i className={system.database === "postgres" ? "on" : ""}>{system.database === "postgres" ? "neon" : "sqlite"}</i>
            <i className={system.llm.provider ? "on" : "off"}>{system.llm.provider ? system.llm.model : "no llm key"}</i>
            <i className={system.github ? "on" : "off"}>github</i>
          </span>
        )}
        <span className={`mode mono ${connection}`} title="Connection to the Chowkidaar backend">
          {connection === "live" ? "live" : connection === "connecting" ? "connecting" : "backend offline · retrying"}
        </span>
        {hasProject && <Bell />}
        {hasProject && (
          <button className="search-btn" onClick={() => setSearch(true)}>
            <Icon.search /> <span>Find a node</span> <kbd>/</kbd>
          </button>
        )}
      </div>
    </header>
  );
}

/** Every project has its own graph, its own runs, and its own agent. */
function Projects() {
  const repos = useStore((s) => s.repos);
  const agents = useStore((s) => s.agents);
  const projectId = useStore((s) => s.projectId);
  const { openProject, setAddingProject } = useStore.getState();
  const [open, setOpen] = useState(false);
  const current = repos.find((r) => r.id === projectId);
  return (
    <div className="projects">
      <button className="project-btn" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <span className="muted mono">project</span> <b>{current?.name ?? "…"}</b> <Icon.chevron />
      </button>
      {open && (
        <div className="panel popover">
          {repos.map((r) => {
            const agent = agents.find((a) => a.repoId === r.id);
            return (
              <button key={r.id} className={`project${r.id === projectId ? " is-on" : ""}`} onClick={() => { openProject(r.id); setOpen(false); }}>
                <span className={`hex ${r.open_issues ? "coral" : "mint"}`} />
                <span className="integration-main">
                  <b>{r.name}</b>
                  <small className="mono">{r.integrations.length} API{r.integrations.length === 1 ? "" : "s"} · {r.connected_via === "connector" ? "connector" : "path"}{r.full_name ? ` · ${r.full_name}` : ""}</small>
                </span>
                <span className={`state ${agent?.status === "working" ? "mint" : agent?.status === "waiting" ? "amber" : r.open_issues ? "coral" : "muted"}`}>
                  {agent?.status === "working" ? "agent working" : agent?.status === "waiting" ? "needs you" : r.open_issues ? `${r.open_issues} open` : "healthy"}
                </span>
              </button>
            );
          })}
          <button className="btn" onClick={() => { setAddingProject(true); setOpen(false); }}><Icon.plus /> Add a project</button>
        </div>
      )}
    </div>
  );
}

export function CanvasControls() {
  const follow = useStore((s) => s.follow);
  const { zoom, fit, setFollow } = useStore.getState();
  return (
    <div className="controls">
      <button className={`icon-btn${follow ? " is-on" : ""}`} onClick={() => setFollow(!follow)} title="Camera follows the pipeline">
        <Icon.follow />
      </button>
      <i />
      <button className="icon-btn" onClick={() => zoom(1.4)} title="Zoom in"><Icon.plus /></button>
      <button className="icon-btn" onClick={() => zoom(1 / 1.4)} title="Zoom out"><Icon.minus /></button>
      <button className="icon-btn" onClick={fit} title="Fit the whole graph (F)"><Icon.fit /></button>
    </div>
  );
}

export function Search() {
  const open = useStore((s) => s.searchOpen);
  const nodes = useStore((s) => s.graph.nodes);
  const { setSearch, select, setFocus } = useStore.getState();
  const [q, setQ] = useState("");
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  const results = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return [];
    return nodes
      .map((n) => ({ n, i: n.label.toLowerCase().indexOf(needle) }))
      .filter((x) => x.i >= 0)
      .sort((a, b) => a.i - b.i || a.n.label.length - b.n.label.length)
      .slice(0, 8)
      .map((x) => x.n);
  }, [q, nodes]);

  useEffect(() => {
    if (open) {
      setQ("");
      setCursor(0);
      inputRef.current?.focus();
    } else setFocus(null);
  }, [open, setFocus]);
  useEffect(() => {
    if (open) setFocus(results[cursor] ? [results[cursor].id] : null);
  }, [open, results, cursor, setFocus]);

  if (!open) return null;
  const choose = (id: string) => {
    setSearch(false);
    select(id, { fly: true });
  };
  return (
    <div className="search-veil" onPointerDown={(e) => e.target === e.currentTarget && setSearch(false)}>
      <div className="panel search">
        <div className="search-input">
          <Icon.search />
          <input
            ref={inputRef}
            value={q}
            placeholder="File, function, class or provider"
            onChange={(e) => {
              setQ(e.target.value);
              setCursor(0);
            }}
            onKeyDown={(e) => {
              if (e.key === "ArrowDown") setCursor((c) => Math.min(results.length - 1, c + 1));
              else if (e.key === "ArrowUp") setCursor((c) => Math.max(0, c - 1));
              else if (e.key === "Enter" && results[cursor]) choose(results[cursor].id);
              else if (e.key === "Escape") setSearch(false);
              else return;
              e.preventDefault();
            }}
          />
          <kbd>esc</kbd>
        </div>
        {results.map((n, i) => (
          <button key={n.id} className={`search-row${i === cursor ? " is-on" : ""}`} onMouseEnter={() => setCursor(i)} onClick={() => choose(n.id)}>
            <span className={`dot kind-${n.kind}`} style={{ "--c": n.kind === "provider" ? "var(--mint)" : communityColor(n.community) } as React.CSSProperties} />
            <span className="mono">{shortLabel(n.label)}</span>
            <span className="muted small">{n.kind} · {n.communityName}</span>
          </button>
        ))}
        {q && results.length === 0 && <p className="search-none">Nothing in the graph matches “{q}”.</p>}
      </div>
    </div>
  );
}
