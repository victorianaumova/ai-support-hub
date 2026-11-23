import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "../api/client";
import type { components, paths } from "../api/schema";

export type QueueRow = components["schemas"]["QueueTicketOut"];
type Status = components["schemas"]["TicketStatus"];
type QueueQuery = NonNullable<paths["/api/v1/console/tickets"]["get"]["parameters"]["query"]>;

export const TABS = [
  { id: "approval", label: "Needs approval", status: ["pending_approval"] as Status[], assigned: "any" },
  { id: "handoff", label: "Handed off", status: ["handed_off"] as Status[], assigned: "any" },
  { id: "mine", label: "Mine", status: [] as Status[], assigned: "me" },
  { id: "answered", label: "Answered", status: ["answered"] as Status[], assigned: "any" },
  { id: "all", label: "All", status: [] as Status[], assigned: "any" },
] as const;
export type TabId = (typeof TABS)[number]["id"];

export interface Filters {
  intent: string;
  urgency: string;
  reason: string;
  q: string;
}

export const REFRESH_MS = 5_000;
const ORDER_KEY = "hub.console.queue-order";

/** The rows of one tab (first page, 50 rows), refreshed every 5 s (UI §3). */
export function useQueue(tab: TabId, filters: Filters) {
  const spec = TABS.find((t) => t.id === tab) ?? TABS[0];
  return useQuery({
    queryKey: ["console", "queue", tab, filters],
    refetchInterval: REFRESH_MS,
    queryFn: async () => {
      const params: { query: QueueQuery } = {
        query: {
          assigned: spec.assigned,
          limit: 50,
          ...(spec.status.length > 0 ? { status: spec.status } : {}),
          ...(filters.intent ? { intent: filters.intent as components["schemas"]["Intent"] } : {}),
          ...(filters.urgency ? { urgency: filters.urgency as components["schemas"]["Urgency"] } : {}),
          ...(filters.reason ? { route_reason: filters.reason } : {}),
          ...(filters.q.trim() ? { q: filters.q.trim() } : {}),
        },
      };
      return unwrap(await api.GET("/api/v1/console/tickets", { params }));
    },
  });
}

/** The order of the queue the operator last saw, for J / K in the workspace. */
export function rememberOrder(ids: string[]): void {
  try {
    sessionStorage.setItem(ORDER_KEY, JSON.stringify(ids));
  } catch {
    // storage blocked: J / K do nothing
  }
}

export function neighbour(id: string, step: 1 | -1): string | null {
  try {
    const ids = JSON.parse(sessionStorage.getItem(ORDER_KEY) ?? "[]") as string[];
    const index = ids.indexOf(id);
    return index === -1 ? null : (ids[index + step] ?? null);
  } catch {
    return null;
  }
}
