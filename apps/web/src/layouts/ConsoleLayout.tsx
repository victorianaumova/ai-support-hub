import { NavLink, Outlet, useNavigate } from "react-router";
import { AREA_ROLES, hasRole, type Role, useLogout, useOperator } from "../auth/session";
import { Icon } from "../components/Icon";
import type { IconName } from "../components/icons.generated";
import { DemoBadge, Logo } from "../components/Logo";

const NAV: { to: string; label: string; icon: IconName; roles: readonly Role[]; end?: boolean }[] = [
  { to: "/console", label: "Queue", icon: "inbox", roles: AREA_ROLES.queue, end: true },
  { to: "/console/kb", label: "Knowledge base", icon: "menu_book", roles: AREA_ROLES.kb },
  { to: "/console/dashboard", label: "Dashboard", icon: "monitoring", roles: AREA_ROLES.dashboard },
  { to: "/console/settings", label: "Settings", icon: "settings", roles: AREA_ROLES.settings },
  { to: "/console/audit", label: "Audit", icon: "history", roles: AREA_ROLES.audit },
];

export function ConsoleLayout() {
  const { data: operator } = useOperator();
  const logout = useLogout();
  const navigate = useNavigate();
  return (
    <div className="flex min-h-dvh flex-col bg-surface">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-20 focus:rounded focus:bg-white focus:p-2">
        Skip to content
      </a>
      <header className="sticky top-0 z-10 border-b border-line bg-white">
        <div className="flex h-14 items-center gap-3 px-4">
          <Logo product="Support console" />
          <DemoBadge />
          {operator && (
            <div className="ml-auto flex items-center gap-3">
              <span className="hidden items-center gap-2 sm:flex">
                <Icon name="person" className="text-muted" />
                <span>
                  {operator.name} <span className="text-muted">· {operator.role}</span>
                </span>
              </span>
              <button
                type="button"
                className="btn btn-secondary"
                disabled={logout.isPending}
                onClick={() => {
                  logout.mutate(undefined, { onSettled: () => void navigate("/console/login") });
                }}
              >
                <Icon name="logout" size={18} />
                Log out
              </button>
            </div>
          )}
        </div>
      </header>
      <div className="flex flex-1 flex-col md:flex-row">
        {operator && (
          <nav aria-label="Console" className="border-b border-line bg-white md:w-56 md:shrink-0 md:border-r md:border-b-0">
            <ul className="flex gap-1 overflow-x-auto p-2 md:sticky md:top-14 md:flex-col">
              {NAV.filter((item) => hasRole(operator, item.roles)).map((item) => (
                <li key={item.to}>
                  <NavLink
                    to={item.to}
                    end={item.end ?? false}
                    className={({ isActive }) =>
                      `flex h-10 items-center gap-3 whitespace-nowrap rounded-full px-4 font-medium md:rounded-l-none ${isActive ? "bg-primary-weak text-[var(--tb-primary-strong)]" : "text-ink hover:bg-surface-hover"}`
                    }
                  >
                    <Icon name={item.icon} />
                    {item.label}
                  </NavLink>
                </li>
              ))}
            </ul>
          </nav>
        )}
        <main id="main" className="min-w-0 flex-1">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
