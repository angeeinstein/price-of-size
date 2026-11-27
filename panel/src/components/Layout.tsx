import { useQueryClient } from "@tanstack/react-query";
import {
  Activity, Boxes, ChartScatter, Code, Euro, FolderTree, Globe, KeyRound, LayoutDashboard, ListChecks, LogOut,
  Moon, Package, Settings, Sun, Users,
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { NavLink } from "react-router";
import { api, setCsrf } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { applyTheme, initialDark } from "@/lib/theme";
import { cn } from "@/lib/utils";

const NAV = [
  { to: "/", label: "Overview", icon: LayoutDashboard },
  { to: "/nodes", label: "Nodes", icon: Boxes },
  { to: "/jobs", label: "Jobs", icon: ListChecks },
  { to: "/domains", label: "Domains", icon: Globe },
  { to: "/sources", label: "Sources", icon: Activity },
  { to: "/categories", label: "Categories", icon: FolderTree },
  { to: "/prices", label: "Price checks", icon: Euro },
  { to: "/parsers", label: "Parsers", icon: Code },
  { to: "/explorer", label: "Explorer", icon: ChartScatter },
  { to: "/releases", label: "Releases", icon: Package },
  { to: "/settings", label: "Settings", icon: Settings },
  { to: "/users", label: "Users", icon: Users, admin: true },
];

export function Layout({ children }: { children: ReactNode }) {
  const { me } = useAuth();
  const qc = useQueryClient();
  const [dark, setDark] = useState(initialDark);
  useEffect(() => applyTheme(dark), [dark]);

  async function logout() {
    await api.post("/api/panel/logout");
    setCsrf(null);
    qc.clear();
    window.location.assign("/");
  }

  return (
    <div className="flex h-full">
      <aside className="flex w-56 shrink-0 flex-col border-r border-line bg-panel">
        <div className="px-5 py-4">
          <div className="text-sm font-semibold">The Price of Size</div>
          <div className="text-xs text-muted">control panel</div>
        </div>
        <nav className="flex-1 space-y-0.5 px-2">
          {NAV.filter((n) => !n.admin || me?.user.role === "admin").map(({ to, label, icon: Icon }) => (
            <NavLink key={to} to={to} end={to === "/"}
              className={({ isActive }) => cn(
                "flex items-center gap-2 rounded-md px-3 py-2 text-sm",
                isActive ? "bg-accent/10 font-medium text-accent" : "text-muted hover:bg-bg hover:text-fg",
              )}>
              <Icon size={16} /> {label}
            </NavLink>
          ))}
        </nav>
        <div className="space-y-1 border-t border-line p-2">
          <NavLink to="/account" className="flex items-center gap-2 rounded-md px-3 py-2 text-sm text-muted hover:bg-bg hover:text-fg">
            <KeyRound size={16} /> {me?.user.username}
            <span className="ml-auto text-xs">{me?.user.role}</span>
          </NavLink>
          <div className="flex gap-1">
            <button onClick={() => setDark(!dark)} aria-label="Toggle dark mode"
              className="flex flex-1 items-center justify-center gap-2 rounded-md px-3 py-2 text-sm text-muted hover:bg-bg">
              {dark ? <Sun size={16} /> : <Moon size={16} />}
            </button>
            <button onClick={logout} aria-label="Log out"
              className="flex flex-1 items-center justify-center gap-2 rounded-md px-3 py-2 text-sm text-muted hover:bg-bg">
              <LogOut size={16} />
            </button>
          </div>
        </div>
      </aside>
      <main className="min-w-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-7xl p-6">{children}</div>
      </main>
    </div>
  );
}

export function PageTitle({ title, subtitle, action }: { title: string; subtitle?: string; action?: ReactNode }) {
  return (
    <div className="mb-6 flex items-end justify-between gap-4">
      <div>
        <h1 className="text-xl font-semibold">{title}</h1>
        {subtitle && <p className="text-sm text-muted">{subtitle}</p>}
      </div>
      {action}
    </div>
  );
}
