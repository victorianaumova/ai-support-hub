import { ApiError, describe } from "../api/problem";

/** An error a person can act on: plain words, a retry, and the request id to quote. */
export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const requestId = error instanceof ApiError ? error.requestId : null;
  return (
    <div role="alert" className="m-4 rounded-lg border border-red-300 bg-red-50 p-4 text-red-900">
      <p className="font-medium">
        <span aria-hidden="true">⚠ </span>
        {describe(error)}
      </p>
      {requestId !== null && <p className="mt-1 text-sm">Reference: {requestId}</p>}
      {onRetry !== undefined && (
        <button type="button" onClick={onRetry} className="mt-3 btn btn-danger">
          Try again
        </button>
      )}
    </div>
  );
}
