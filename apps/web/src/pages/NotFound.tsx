import { Link } from "react-router";

export function NotFound() {
  return (
    <div className="mx-auto max-w-xl px-4 py-12">
      <h1 className="text-2xl font-semibold">Page not found</h1>
      <p className="mt-2 text-muted">
        <Link to="/" className="text-brand-700 underline">
          Back to the start page
        </Link>
      </p>
    </div>
  );
}
