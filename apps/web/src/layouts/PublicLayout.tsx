import { Link, Outlet } from "react-router";

export function PublicLayout() {
  return (
    <div className="flex min-h-dvh flex-col">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:rounded focus:bg-white focus:p-2">
        Skip to content
      </a>
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-4 py-3">
          <Link to="/" className="font-semibold text-brand-700">
            Tallybridge Support Hub
          </Link>
          <span className="rounded bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-900">Demo · fictional company</span>
        </div>
      </header>
      <main id="main" className="flex-1">
        <Outlet />
      </main>
    </div>
  );
}
