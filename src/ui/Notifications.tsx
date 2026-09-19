import { useEffect, useState } from "react";
import { api } from "../data/api";
import { useStore } from "../lib/store";
import type { Notice } from "../lib/types";
import { ago, Icon } from "./shared";

const TONE: Record<Notice["level"], string> = { info: "blue", success: "mint", warning: "coral", action: "amber" };

export function Bell() {
  const notices = useStore((s) => s.notices);
  const openProject = useStore((s) => s.openProject);
  const [open, setOpen] = useState(false);
  const unread = notices.filter((n) => !n.read).length;
  const toggle = () => {
    setOpen((o) => !o);
    if (!open && unread) {
      api.markRead().catch(() => {});
      useStore.getState().setNotices(notices.map((n) => ({ ...n, read: true })));
    }
  };
  return (
    <div className="bell">
      <button className="icon-btn" onClick={toggle} title="Notifications" aria-expanded={open}>
        <Icon.bell />
        {unread > 0 && <i className="badge">{unread}</i>}
      </button>
      {open && (
        <div className="panel popover notices">
          <h3 className="panel-title">Notifications<i>{notices.length}</i></h3>
          {notices.length === 0 && <p className="feed-empty mono">nothing yet</p>}
          {notices.map((n) => (
            <button key={n.id} className="notice" onClick={() => { if (n.repoId) openProject(n.repoId); setOpen(false); }}>
              <span className={`dot`} style={{ "--c": `var(--${TONE[n.level]})` } as React.CSSProperties} />
              <span className="notice-main"><b>{n.title}</b>{n.body && <small>{n.body}</small>}</span>
              <time className="mono">{ago(n.createdAt)}</time>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function Toasts() {
  const toasts = useStore((s) => s.toasts);
  return (
    <div className="toasts" aria-live="polite">
      {toasts.map((t) => <Toast key={t.id} notice={t} />)}
    </div>
  );
}

function Toast({ notice }: { notice: Notice }) {
  const dismiss = useStore((s) => s.dismissToast);
  const openProject = useStore((s) => s.openProject);
  const repo = useStore((s) => s.repos.find((r) => r.id === notice.repoId)?.name);
  useEffect(() => {
    // A decision the user has to make stays until it is seen.
    if (notice.level === "action") return;
    const t = setTimeout(() => dismiss(notice.id), 7000);
    return () => clearTimeout(t);
  }, [notice, dismiss]);
  return (
    <div className={`panel toast rise is-${TONE[notice.level]}`}>
      <button className="toast-main" onClick={() => { if (notice.repoId) openProject(notice.repoId); dismiss(notice.id); }}>
        <span className={`eyebrow ${TONE[notice.level]}`}>{repo ?? "Chowkidaar"}</span>
        <b>{notice.title}</b>
        {notice.body && <small>{notice.body}</small>}
      </button>
      <button className="icon-btn" onClick={() => dismiss(notice.id)} title="Dismiss"><Icon.x /></button>
    </div>
  );
}
