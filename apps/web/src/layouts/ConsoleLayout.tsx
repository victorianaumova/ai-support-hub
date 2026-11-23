import { NavLink, Outlet, useNavigate } from "react-router";
import { AREA_ROLES, hasRole, type Role, useLogout, useOperator } from "../auth/session";

const NAV: { to: string; label: string; roles: readonly Role[]; end?: boolean }[] = [
  { to: "/console", label: "Queue", roles: AREA_ROLES.queue, end: true },
  { to: "/console/kb", label: "Knowledge base", roles: AREA_ROLES.kb },
  { to: "/console/dashboard", label: "Dashboard", roles: AREA_ROLES.dashboard },
  { to: "/console/settings", label: "Settings", roles: AREA_ROLES.settings },
  { to: "/console/audit", label: "Audit", roles: AREA_ROLES.audit },
];

export function ConsoleLayout() {
  const { data: operator } = useOperator();
  const logout = useLogout();
  const navigate = useNavigate();
  return (
    <div className="flex min-h-dvh flex-col">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:rounded focus:bg-white focus:p-2">
        Skip to content
      </a>
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3">
          <span className="font-semibold text-brand-700">Support console</span>
          {operator && (
            <nav aria-label="Console" className="flex flex-wrap gap-1">
              {NAV.filter((item) => hasRole(operator, item.roles)).map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end ?? false}
                  className={({ isActive }) =>
                    `rounded-md px-3 py-1.5 text-sm ${isActive ? "bg-brand-50 font-semibold text-brand-700 underline underline-offset-4" : "text-muted hover:bg-slate-100"}`
                  }
                >
                  {item.label}
                </NavLink>
              ))}
            </nav>
          )}
          {operator && (
            <div className="ml-auto flex items-center gap-3 text-sm">
              <span>
                {operator.name} <span className="text-muted">· {operator.role}</span>
              </span>
              <button
                type="button"
                className="rounded-md border border-slate-300 px-3 py-1.5 hover:bg-slate-100"
                disabled={logout.isPending}
                onClick={() => {
                  logout.mutate(undefined, { onSettled: () => void navigate("/console/login") });
                }}
              >
                Log out
              </button>
            </div>
          )}
        </div>
      </header>
      <main id="main" className="flex-1">
        <Outlet />
      </main>
    </div>
  );
}
