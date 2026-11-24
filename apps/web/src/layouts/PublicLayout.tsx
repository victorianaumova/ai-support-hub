import { Link, Outlet } from "react-router";
import { DemoBadge, FictionalNote, Logo } from "../components/Logo";

export function PublicLayout() {
  return (
    <div className="flex min-h-dvh flex-col bg-shell">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:rounded focus:bg-white focus:p-2">
        Skip to content
      </a>
      <header className="bg-shell">
        <div className="flex h-16 items-center gap-4 px-4">
          <Link to="/" className="rounded-md">
            <Logo product="Support Hub" />
          </Link>
          <DemoBadge />
        </div>
      </header>
      <main id="main" className="flex-1">
        <Outlet />
      </main>
      <footer className="bg-shell">
        <div className="px-4 py-4">
          <FictionalNote />
        </div>
      </footer>
    </div>
  );
}
