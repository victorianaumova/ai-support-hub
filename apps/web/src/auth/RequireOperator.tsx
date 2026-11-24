import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router";
import { ErrorState } from "../components/ErrorState";
import { Loading } from "../components/Loading";
import { hasRole, type Role, useOperator } from "./session";

/** Shows its children to a logged-in operator with one of `roles`; else login or "no access". */
export function RequireOperator({ roles, children }: { roles: readonly Role[]; children: ReactNode }) {
  const session = useOperator();
  const location = useLocation();
  if (session.isPending) return <Loading label="Checking your session" />;
  if (session.isError) return <ErrorState error={session.error} onRetry={() => void session.refetch()} />;
  if (session.data === null) {
    const next = encodeURIComponent(location.pathname + location.search);
    return <Navigate to={`/console/login?next=${next}`} replace />;
  }
  if (!hasRole(session.data, roles)) {
    return (
      <div role="alert" className="mx-auto max-w-xl p-6">
        <h1 className="page-title">No access</h1>
        <p className="mt-2 text-muted">Your role ({session.data.role}) cannot open this page.</p>
      </div>
    );
  }
  return children;
}
