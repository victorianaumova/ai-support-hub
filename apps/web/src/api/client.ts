import createClient, { type Middleware } from "openapi-fetch";
import { apiError } from "./problem";
import type { paths } from "./schema";

/**
 * The CSRF token of the operator session (api.md §1). It is kept in memory only: a reload gets
 * it again from `GET /auth/me`. Never in localStorage, so a script cannot read it later.
 */
let csrfToken: string | null = null;

export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

const csrf: Middleware = {
  onRequest({ request }) {
    if (csrfToken !== null && !SAFE_METHODS.has(request.method)) {
      request.headers.set("X-CSRF-Token", csrfToken);
    }
    return request;
  },
};

/** Same origin: the cookies are first-party (ADR-0016). The origin is spelt out because `Request`
 * outside a browser page (tests) does not resolve a relative URL. */
export const api = createClient<paths>({
  baseUrl: window.location.origin,
  credentials: "same-origin",
  // Looked up on every call (not captured once), so tests can stub `fetch`.
  fetch: (request: Request) => globalThis.fetch(request),
});
api.use(csrf);

/** The data of an answer, or an `ApiError` thrown for TanStack Query to show. */
export function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (!result.response.ok || result.data === undefined) {
    throw apiError(result.response, result.error);
  }
  return result.data;
}

/** `Idempotency-Key` for POSTs that create a message or run an action (api.md §1). */
export function idempotencyKey(): string {
  return crypto.randomUUID();
}
