import { isRouteErrorResponse, useRouteError } from "react-router";
import { ErrorState } from "../components/ErrorState";
import { NotFound } from "../pages/NotFound";

/** Last line: an error a page did not catch shows a message, never a blank screen. */
export function RouteError() {
  const error = useRouteError();
  if (isRouteErrorResponse(error) && error.status === 404) return <NotFound />;
  return (
    <ErrorState
      error={error}
      onRetry={() => {
        window.location.reload();
      }}
    />
  );
}
