import { QueryClient } from "@tanstack/react-query";
import { ApiError } from "../api/problem";

function shouldRetry(failureCount: number, error: unknown): boolean {
  // A 4xx answer will not change on retry; a network error or 5xx may.
  if (error instanceof ApiError && error.status < 500) return false;
  return failureCount < 2;
}

export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: shouldRetry, refetchOnWindowFocus: false, staleTime: 5_000 },
      mutations: { retry: false },
    },
  });
}
