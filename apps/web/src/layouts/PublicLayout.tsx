import { Link, Outlet } from "react-router";
import { DemoBadge, FictionalNote, Logo } from "../components/Logo";

export function PublicLayout() {
  return (
    <div className="flex min-h-dvh flex-col bg-surface">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:rounded focus:bg-white focus:p-2">
        Skip to content
      </a>
      <header className="border-b border-line bg-white">
        <div className="mx-auto flex h-14 max-w-6xl items-center gap-3 px-4">
          <Link to="/" className="rounded-md">
            <Logo product="Support Hub" />
          </Link>
          <DemoBadge />
        </div>
      </header>
      <main id="main" className="flex-1">
        <Outlet />
      </main>
      <footer className="border-t border-line bg-white">
        <div className="mx-auto max-w-6xl px-4 py-4">
          <FictionalNote />
        </div>
      </footer>
    </div>
  );
}
