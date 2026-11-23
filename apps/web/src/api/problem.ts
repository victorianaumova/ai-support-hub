/** RFC 9457 problem details as the API sends them (api.md §1). */
export interface Problem {
  status: number;
  title: string;
  detail: string;
  code: string;
  errors?: { loc: (string | number)[]; type: string; msg: string }[];
}

/** An error answer of the API, with the request id to quote when asking for help. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | null;
  readonly retryAfterS: number | null;
  readonly problem: Problem | null;

  constructor(status: number, problem: Problem | null, requestId: string | null, retryAfterS: number | null) {
    super(problem?.detail ?? `Request failed with status ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.code = problem?.code ?? `http_${status}`;
    this.problem = problem;
    this.requestId = requestId;
    this.retryAfterS = retryAfterS;
  }
}

function isProblem(value: unknown): value is Problem {
  if (typeof value !== "object" || value === null) return false;
  const record = value as Record<string, unknown>;
  return typeof record.status === "number" && typeof record.code === "string";
}

function retryAfter(response: Response): number | null {
  const value = response.headers.get("Retry-After");
  if (value === null) return null;
  const seconds = Number(value);
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : null;
}

export function apiError(response: Response, body: unknown): ApiError {
  return new ApiError(
    response.status,
    isProblem(body) ? body : null,
    response.headers.get("X-Request-ID"),
    retryAfter(response),
  );
}

/** Words for a person, never a stack trace or internal detail. */
export function describe(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 429) return "Too many requests. Please wait a moment and try again.";
    if (error.status === 401) return "Your session has ended. Please sign in again.";
    if (error.status === 403) return "You do not have access to this page.";
    if (error.status >= 500) return "Something went wrong on our side. Please try again.";
    return error.problem?.detail ?? "The request could not be completed.";
  }
  return "The connection failed. Check your network and try again.";
}
