import { useState } from "react";
import { api } from "../data/api";
import { refresh } from "../data/source";
import { useStore } from "../lib/store";
import { Icon, Spinner } from "./shared";

// First run: name the workspace, get the API key (shown once), connect a project.
// The same last step is reused for "add a project" later.
export function Onboarding() {
  const workspace = useStore((s) => s.workspace);
  const apiKey = useStore((s) => s.apiKey);
  const repos = useStore((s) => s.repos);
  const adding = useStore((s) => s.addingProject);
  const step = !workspace?.onboarded ? 1 : 2;
  return (
    <div className="onboard">
      <div className="panel onboard-card rise" key={step}>
        <ol className="onboard-steps mono" aria-label="Setup progress">
          <li className={step === 1 ? "is-now" : "is-done"}>1 · Workspace</li>
          <li className={step === 2 ? "is-now" : ""}>2 · API key & project</li>
          <li>3 · Pipeline graph</li>
        </ol>
        {step === 1 ? <CreateWorkspace /> : <ConnectProject apiKey={apiKey} first={repos.length === 0} />}
        {adding && repos.length > 0 && (
          <button className="onboard-close icon-btn" onClick={() => useStore.getState().setAddingProject(false)} title="Back to your projects"><Icon.x /></button>
        )}
      </div>
    </div>
  );
}

function CreateWorkspace() {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    api.onboard(name.trim() || "My workspace")
      .then((w) => { useStore.getState().setApiKey(w.api_key ?? null); return refresh(); })
      .catch((err: Error) => setError(err.message))
      .finally(() => setBusy(false));
  };
  return (
    <form onSubmit={submit} className="onboard-body">
      <h1>APIs that maintain their own integrations</h1>
      <p>Chowkidaar watches the external APIs your code depends on. When one changes, an agent finds the code it reaches, fixes it, proves the fix with your own checks, and opens the pull request.</p>
      <label className="field">
        <span className="eyebrow muted">Name your workspace</span>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Acme engineering" autoFocus />
      </label>
      <button className="cta cta-mint" disabled={busy}>
        <span>Create workspace<small>Generates the API key your projects connect with</small></span>
        {busy ? <Spinner /> : <Icon.arrow />}
      </button>
      {error && <p className="error">{error}</p>}
    </form>
  );
}

function Copy({ text, label }: { text: string; label: string }) {
  const [done, setDone] = useState(false);
  return (
    <button type="button" className="copy" onClick={() => navigator.clipboard.writeText(text).then(() => { setDone(true); setTimeout(() => setDone(false), 1600); })}>
      {done ? <Icon.check /> : <Icon.layers />} {done ? "Copied" : label}
    </button>
  );
}

function ConnectProject({ apiKey, first }: { apiKey: string | null; first: boolean }) {
  const workspace = useStore((s) => s.workspace)!;
  const [target, setTarget] = useState("");
  const [busy, setBusy] = useState<"rotate" | "connect" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const command = (workspace.connect_command ?? "").replace("<your API key>", apiKey ?? "<your API key>");

  const rotate = () => {
    setBusy("rotate");
    api.rotateKey().then((w) => useStore.getState().setApiKey(w.api_key ?? null)).catch((e: Error) => setError(e.message)).finally(() => setBusy(null));
  };
  const connect = (e: React.FormEvent) => {
    e.preventDefault();
    if (!target.trim()) return;
    setBusy("connect");
    setError(null);
    api.connectRepo(target.trim())
      .then((repo) => { useStore.getState().setAddingProject(false); useStore.getState().openProject(repo.id); return refresh(); })
      .catch((err: Error) => setError(err.message))
      .finally(() => setBusy(null));
  };

  return (
    <div className="onboard-body">
      <h1>{first ? "Connect your first project" : "Add a project"}</h1>
      {apiKey ? (
        <div className="keybox">
          <span className="eyebrow amber">your API key · shown once</span>
          <code className="mono">{apiKey}</code>
          <Copy text={apiKey} label="Copy key" />
          <p className="hint">Only its hash is stored. Keep it somewhere safe; you can always rotate it.</p>
        </div>
      ) : (
        <p className="note">Your key <code className="mono">{workspace.key_prefix}</code> was shown once and is not stored. <button className="link" onClick={rotate} disabled={!!busy}>{busy === "rotate" ? "Rotating…" : "Rotate it"}</button> to get a new one.</p>
      )}

      <section className="group">
        <h3>Run this inside the project</h3>
        <pre className="command mono">{command}</pre>
        <Copy text={command} label="Copy command" />
        <p className="hint">
          It reports the repository and its credential-like environment variables. Values never leave your machine: each is reduced there to a keyed hash, and only that fingerprint is sent. <code className="mono">--watch</code> keeps reporting env changes.
        </p>
      </section>

      <p className="waiting mono"><Spinner /> waiting for a project to connect</p>

      <form className="group" onSubmit={connect}>
        <h3>Or point Chowkidaar at it</h3>
        <div className="inline">
          <input className="mono" value={target} onChange={(e) => setTarget(e.target.value)} placeholder="owner/repo   or   /path/to/checkout" spellCheck={false} />
          <button className="btn" disabled={!target.trim() || !!busy}>{busy === "connect" ? <Spinner /> : <Icon.arrow />} Connect</button>
        </div>
      </form>
      {error && <p className="error">{error}</p>}
    </div>
  );
}
