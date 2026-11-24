import { Link, Outlet } from "react-router";
import { DemoBadge, FictionalNote, Logo } from "../components/Logo";

/** Public frame: the same lavender canvas and rounded app card as the console. */
export function PublicLayout() {
  return (
    <div className="min-h-dvh bg-canvas md:p-4 lg:p-6">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-20 focus:rounded-full focus:bg-white focus:px-4 focus:py-2">
        Skip to content
      </a>
      <div className="flex min-h-dvh flex-col bg-shell md:min-h-[calc(100dvh-2rem)] md:rounded-sheet md:shadow-[var(--tb-frame-shadow)] lg:min-h-[calc(100dvh-3rem)]">
        <header>
          <div className="flex h-20 items-center gap-4 px-4 sm:px-8">
            <Link to="/" className="rounded-xl">
              <Logo product="Support Hub" />
            </Link>
            <DemoBadge />
          </div>
        </header>
        <main id="main" className="flex-1">
          <Outlet />
        </main>
        <footer>
          <div className="px-4 py-5 sm:px-8">
            <FictionalNote />
          </div>
        </footer>
      </div>
    </div>
  );
}
