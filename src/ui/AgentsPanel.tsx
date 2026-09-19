import { useStore } from "../lib/store";
import { Spinner } from "./shared";

// One private agent per project. This lists the ones that are busy or waiting on the user, across all projects.
export function AgentsPanel() {
  const agents = useStore((s) => s.agents);
  const projectId = useStore((s) => s.projectId);
  const openProject = useStore((s) => s.openProject);
  const active = agents.filter((a) => a.status !== "idle").sort((a, b) => (a.status === b.status ? 0 : a.status === "working" ? -1 : 1));
  const idle = agents.length - active.length;
  return (
    <section className="panel agents">
      <h3 className="panel-title">Agents working<i>{active.filter((a) => a.status === "working").length}</i></h3>
      {active.length === 0 && <p className="feed-empty mono">{agents.length ? `${idle} agent${idle === 1 ? "" : "s"} idle · watching` : "no agents yet"}</p>}
      {active.map((a) => (
        <button key={a.repoId} className={`agent is-${a.status}${a.repoId === projectId ? " is-here" : ""}`} onClick={() => openProject(a.repoId)} title={`Open ${a.repoName}`}>
          {a.status === "working" ? <Spinner /> : <span className="agent-wait" />}
          <span className="agent-main">
            <b>{a.repoName}</b>
            <small>{a.detail ?? a.task}</small>
          </span>
          <span className={`state ${a.status === "working" ? "mint" : "amber"}`}>{a.status === "working" ? "working" : "needs you"}</span>
        </button>
      ))}
      {active.length > 0 && idle > 0 && <p className="agents-idle mono">{idle} more idle · each with its own context</p>}
    </section>
  );
}
