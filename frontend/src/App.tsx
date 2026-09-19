import { useEffect } from "react";
import { GraphCanvas } from "./graph/GraphCanvas";
import { useStore } from "./lib/store";
import { AgentsPanel } from "./ui/AgentsPanel";
import { CanvasControls, Search, TopBar } from "./ui/Chrome";
import { ConfirmPrompt } from "./ui/ConfirmPrompt";
import { LeftColumn } from "./ui/LeftColumn";
import { Onboarding } from "./ui/Onboarding";
import { StagePanel } from "./ui/StagePanel";
import { Toasts } from "./ui/Notifications";

function useShortcuts() {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const s = useStore.getState();
      const typing = (e.target as HTMLElement)?.closest("input, textarea, select");
      if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "f" || e.key === "F") s.fit();
      else if (e.key === "/") s.setSearch(true);
      else if (e.key === "Escape") s.selectedLink ? s.selectLink(null) : s.select(null);
      else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
}

export default function App() {
  useShortcuts();
  const workspace = useStore((s) => s.workspace);
  const hasProject = useStore((s) => s.projectId !== null);
  const adding = useStore((s) => s.addingProject);
  // Until the first project is connected there is nothing to draw: onboarding owns the screen.
  const onboarding = workspace !== null && (!workspace.onboarded || !hasProject || adding);
  return (
    <main className="app">
      <GraphCanvas />
      <TopBar />
      {hasProject && !adding && (
        <>
          <LeftColumn />
          <div className="right">
            <AgentsPanel />
            <StagePanel />
          </div>
          <CanvasControls />
          <ConfirmPrompt />
        </>
      )}
      {onboarding && <Onboarding />}
      <Toasts />
      <Search />
    </main>
  );
}
