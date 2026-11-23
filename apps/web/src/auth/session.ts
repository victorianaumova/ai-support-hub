import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, setCsrfToken, unwrap } from "../api/client";
import { ApiError } from "../api/problem";
import type { components } from "../api/schema";

export type Operator = components["schemas"]["OperatorOut"];
export type Role = Operator["role"];
type SessionOut = components["schemas"]["OperatorSessionOut"];

export const SESSION_KEY = ["auth", "me"] as const;

/** The logged-in operator, or `null` without a session (a 401 is an answer, not an error). */
async function fetchMe(): Promise<Operator | null> {
  const result = await api.GET("/api/v1/auth/me");
  if (result.response.status === 401) {
    setCsrfToken(null);
    return null;
  }
  const session = unwrap(result);
  setCsrfToken(session.csrf_token);
  return session.operator;
}

export function useOperator() {
  return useQuery({ queryKey: SESSION_KEY, queryFn: fetchMe, staleTime: 60_000 });
}

export function useLogin() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (body: { email: string; password: string }): Promise<SessionOut> =>
      unwrap(await api.POST("/api/v1/auth/login", { body })),
    onSuccess: (session) => {
      setCsrfToken(session.csrf_token);
      client.setQueryData(SESSION_KEY, session.operator);
    },
  });
}

export function useLogout() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (): Promise<void> => {
      const result = await api.POST("/api/v1/auth/logout");
      // A session that already ended is logged out too.
      if (!result.response.ok && result.response.status !== 401) {
        throw new ApiError(result.response.status, null, result.response.headers.get("X-Request-ID"), null);
      }
    },
    onSettled: () => {
      setCsrfToken(null);
      client.clear();
      client.setQueryData(SESSION_KEY, null);
    },
  });
}

/** The roles that may open each console area, as the server's rules (`hub/auth/roles.py`). The
 * page only hides what the role cannot use; the server checks every request again. */
export const AREA_ROLES = {
  queue: ["operator", "lead", "admin", "compliance"],
  kb: ["operator", "lead", "admin"],
  dashboard: ["lead", "admin"],
  settings: ["lead", "admin"],
  audit: ["compliance", "lead", "admin"],
} as const satisfies Record<string, readonly Role[]>;

export function hasRole(operator: Operator, allowed: readonly Role[]): boolean {
  return allowed.includes(operator.role);
}
