import { useState } from "react";
import { api } from "../data/api";
import { refresh } from "../data/source";
import { useStore } from "../lib/store";
import { focusProps, Icon, Spinner } from "./shared";

// An environment change that looks like a provider switch. The backend only
// senses it; whether code should follow is the user's call, so nothing runs
// until one of these buttons is pressed.
export function ConfirmPrompt() {
  const pending = useStore((s) => s.pending);
  const resolve = useStore((s) => s.resolvePending);
  const [busy, setBusy] = useState<"confirm" | "dismiss" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [choice, setChoice] = useState("");
  const request = pending[0];
  if (!request) return null;

  const answer = (decision: "confirm" | "dismiss") => {
    setBusy(decision);
    setError(null);
    (decision === "confirm" ? api.confirm(request.id, request.needsProviderChoice ? choice : undefined) : api.dismiss(request.id))
      .then(() => { resolve(request.id); setChoice(""); return refresh(); })
      .catch((e: Error) => setError(e.message))
      .finally(() => setBusy(null));
  };
  const ids = [request.fromProviderId, request.toProviderId].filter((x): x is string => !!x);

  return (
    <section className="panel confirm rise" role="alertdialog" aria-labelledby="confirm-title" {...focusProps(ids)}>
      <span className="eyebrow amber">
        needs your confirmation{pending.length > 1 ? ` · 1 of ${pending.length}` : ""}
      </span>
      <h2 id="confirm-title">{request.title}</h2>
      <p>{request.body}</p>
      {request.needsProviderChoice && (
        <label className="field">
          <span className="eyebrow muted">The new variable is not one Chowkidaar recognises. Which provider is it?</span>
          <select value={choice} onChange={(e) => setChoice(e.target.value)}>
            <option value="">Choose a provider</option>
            {request.providerOptions?.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        </label>
      )}
      {error && <p className="error">{error}</p>}
      <div className="confirm-actions">
        <button className="btn" disabled={!!busy} onClick={() => answer("dismiss")}>
          {busy === "dismiss" ? <Spinner /> : <Icon.x />} Not a switch, leave the code
        </button>
        <button className="btn btn-go" disabled={!!busy || (request.needsProviderChoice && !choice)} onClick={() => answer("confirm")}>
          {busy === "confirm" ? <Spinner /> : <Icon.check />} Confirm and migrate
        </button>
      </div>
      <p className="hint">Secret values never leave your machine. Chowkidaar compared a keyed hash and the variable's name.</p>
    </section>
  );
}
