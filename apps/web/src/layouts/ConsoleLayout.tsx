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

/** Console frame: a lavender canvas around one large rounded app card. The card holds the
 * sidebar (logo, navigation, the signed-in operator) and the page. Under 768px the card fills
 * the screen and the navigation becomes a horizontal strip under the logo row. */
export function ConsoleLayout() {
  const { data: operator } = useOperator();
  const logout = useLogout();
  const navigate = useNavigate();
  return (
    <div className="min-h-dvh bg-canvas md:p-4 lg:p-6">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-20 focus:rounded-full focus:bg-white focus:px-4 focus:py-2">
        Skip to content
      </a>
      <div className="flex min-h-dvh flex-col bg-shell md:min-h-[calc(100dvh-2rem)] md:flex-row md:rounded-sheet md:shadow-[var(--tb-frame-shadow)] lg:min-h-[calc(100dvh-3rem)]">
        <aside className="border-line md:w-68 md:shrink-0 md:border-r">
          <div className="flex flex-wrap items-center gap-x-3 px-4 pt-3 md:sticky md:top-4 md:h-[calc(100dvh-2rem)] md:flex-col md:flex-nowrap md:items-stretch md:px-5 md:pt-7 md:pb-5 lg:top-6 lg:h-[calc(100dvh-3rem)]">
            <div className="flex h-12 flex-1 items-center gap-3 md:flex-none md:px-2">
              <Logo />
              <DemoBadge />
            </div>
            {operator && (
              <>
                <nav aria-label="Console" className="order-3 w-full md:order-2 md:mt-8">
                  <ul className="-mx-1 flex gap-1 overflow-x-auto px-1 py-2 md:mx-0 md:flex-col md:overflow-visible md:p-0">
                    {NAV.filter((item) => hasRole(operator, item.roles)).map((item) => (
                      <li key={item.to}>
                        <NavLink
                          to={item.to}
                          end={item.end ?? false}
                          className={({ isActive }) =>
                            `flex h-11 items-center gap-3 whitespace-nowrap rounded-full px-4 transition-[background-color,color,box-shadow] duration-150 ${isActive ? "bg-white font-medium text-ink shadow-lift" : "text-muted hover:bg-surface-hover hover:text-ink"}`
                          }
                        >
                          <Icon name={item.icon} />
                          {item.label}
                        </NavLink>
                      </li>
                    ))}
                  </ul>
                </nav>
                <div className="order-2 flex items-center gap-3 md:order-3 md:mt-auto md:border-t md:border-line md:pt-5">
                  <span aria-hidden="true" className="flex size-10 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-[#f8dce7] to-[#dce0f8] text-base font-medium text-ink">
                    {operator.name.slice(0, 1)}
                  </span>
                  <span className="hidden min-w-0 flex-1 leading-tight md:block">
                    <span className="block truncate font-medium">{operator.name}</span>
                    <span className="block text-xs text-muted capitalize">{operator.role}</span>
                  </span>
                  <button
                    type="button"
                    aria-label="Log out"
                    title="Log out"
                    className="btn-icon"
                    disabled={logout.isPending}
                    onClick={() => {
                      logout.mutate(undefined, { onSettled: () => void navigate("/console/login") });
                    }}
                  >
                    <Icon name="logout" />
                  </button>
                </div>
              </>
            )}
          </div>
        </aside>
        <main id="main" className="min-w-0 flex-1">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
