import { NavLink, Outlet } from "react-router-dom";
import { Clapperboard, Settings, Users } from "lucide-react";
import { ErrorBoundary } from "./ErrorBoundary";
import { JobTray } from "./JobTray";
import { useEvents } from "../lib/useEvents";

export function Layout() {
  useEvents();

  const navCls = ({ isActive }: { isActive: boolean }) =>
    `flex h-8 items-center gap-1.5 rounded-md px-2.5 text-sm font-medium transition-colors ${
      isActive ? "bg-ink-700/70 text-paper" : "text-fog hover:text-paper"
    }`;

  return (
    <div className="flex min-h-full flex-col">
      <header className="sticky top-0 z-50 border-b border-line bg-ink-950/90 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-7xl items-center gap-6 px-5">
          <NavLink to="/" className="flex items-center gap-2.5">
            <span className="flex h-7 w-7 items-center justify-center rounded-md border border-amber-450/40 bg-amber-450/10">
              <Clapperboard size={15} className="text-amber-450" />
            </span>
            <span className="text-[15px] font-semibold tracking-tight text-paper">
              Story<span className="text-amber-450">Bored</span>
            </span>
          </NavLink>
          <nav className="ml-auto flex items-center gap-1">
            <NavLink to="/" end className={navCls}>
              <Clapperboard size={14} /> Projects
            </NavLink>
            <NavLink to="/characters" className={navCls}>
              <Users size={14} /> Characters
            </NavLink>
            <NavLink to="/settings" className={navCls}>
              <Settings size={14} /> Settings
            </NavLink>
          </nav>
        </div>
      </header>
      <main className="mx-auto w-full max-w-7xl flex-1 px-5 py-8">
        <ErrorBoundary>
          <Outlet />
        </ErrorBoundary>
      </main>
      <ErrorBoundary compact>
        <JobTray />
      </ErrorBoundary>
    </div>
  );
}
