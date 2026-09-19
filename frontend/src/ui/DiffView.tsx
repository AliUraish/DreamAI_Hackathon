import type { ReactNode } from "react";
import type { FilePatch } from "../lib/types";

const TOKEN = /(\/\/.*$|\/\*\*?.*?\*\/)|("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`)|\b(import|from|export|const|async|function|return|await|interface|type|new)\b|\b(\d[\d_.]*)\b/g;

function tint(src: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  for (const m of src.matchAll(TOKEN)) {
    if (m.index > last) out.push(src.slice(last, m.index));
    const cls = m[1] ? "tk-c" : m[2] ? "tk-s" : m[3] ? "tk-k" : "tk-n";
    out.push(<span key={m.index} className={cls}>{m[0]}</span>);
    last = m.index + m[0].length;
  }
  if (last < src.length) out.push(src.slice(last));
  return out;
}

/** Unified diff. Lines reveal top to bottom so a patch reads as being written. */
export function DiffView({ patch, animate = true }: { patch: FilePatch; animate?: boolean }) {
  let i = 0;
  return (
    <div className="diff" key={patch.nodeId}>
      <div className="diff-head">
        <span className="mono">{patch.path}</span>
        <span className="diff-stat">
          <b className="add">+{patch.additions}</b> <b className="del">−{patch.deletions}</b>
        </span>
      </div>
      <div className="diff-body">
        {patch.hunks.map((h) => (
          <div key={h.header}>
            <div className="diff-hunk">{h.header}</div>
            {h.lines.map((l, j) => (
              <div
                key={j}
                className={`diff-line ${l.t === "+" ? "is-add" : l.t === "-" ? "is-del" : ""}${animate ? " reveal" : ""}`}
                style={animate ? { animationDelay: `${i++ * 38}ms` } : undefined}
              >
                <span className="diff-sign">{l.t}</span>
                <code>{tint(l.s) }</code>
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}
