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

/** Console frame: a tinted shell (top bar + nav rail) around a white, rounded content sheet. */
export function ConsoleLayout() {
  const { data: operator } = useOperator();
  const logout = useLogout();
  const navigate = useNavigate();
  return (
    <div className="flex min-h-dvh flex-col bg-shell">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-20 focus:rounded focus:bg-white focus:p-2">
        Skip to content
      </a>
      <header className="sticky top-0 z-10 bg-shell">
        <div className="flex h-16 items-center gap-4 px-4">
          <Logo />
          <DemoBadge />
          {operator && (
            <div className="ml-auto flex items-center gap-3">
              <span className="hidden text-right leading-tight sm:block">
                <span className="block">{operator.name}</span>
                <span className="block text-xs text-muted capitalize">{operator.role}</span>
              </span>
              <span aria-hidden="true" className="flex size-8 items-center justify-center rounded-full bg-primary text-sm font-medium text-white">
                {operator.name.slice(0, 1)}
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
          <nav aria-label="Console" className="md:w-64 md:shrink-0">
            <div className="md:sticky md:top-16">
              <p className="hidden items-center gap-3 px-6 pb-3 pt-2 text-xl md:flex">
                <Icon name="support_agent" size={24} className="text-[var(--tb-text-2)]" />
                Support console
              </p>
              <ul className="flex gap-1 overflow-x-auto px-3 pb-2 md:flex-col md:gap-0.5 md:overflow-visible">
                {NAV.filter((item) => hasRole(operator, item.roles)).map((item) => (
                  <li key={item.to}>
                    <NavLink
                      to={item.to}
                      end={item.end ?? false}
                      className={({ isActive }) =>
                        `flex h-9 items-center gap-3 whitespace-nowrap rounded-full px-4 ${isActive ? "bg-primary-weak font-medium text-primary-strong" : "text-ink hover:bg-surface-hover"}`
                      }
                    >
                      <Icon name={item.icon} />
                      {item.label}
                    </NavLink>
                  </li>
                ))}
              </ul>
            </div>
          </nav>
        )}
        <main id="main" className="min-w-0 flex-1 bg-white md:mb-2 md:mr-2 md:rounded-sheet">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
