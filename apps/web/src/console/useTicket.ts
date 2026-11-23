import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, idempotencyKey, unwrap } from "../api/client";
import { ApiError } from "../api/problem";
import type { components } from "../api/schema";

export type Ticket = components["schemas"]["ConsoleTicketOut"];
export type Draft = components["schemas"]["DraftOut"];
export type RejectReason = components["schemas"]["RejectReason"];

export interface Citation {
  marker: number;
  source_id?: string;
  doc_id?: string;
  title?: string;
  url?: string;
}
export interface EvidenceChunk {
  chunk_id: string;
  doc_id: string;
  title: string;
  section: string;
  url: string;
  score: number;
  rank: number;
  text: string;
}
export interface AccountFact {
  fact_id: string;
  source_tool: string;
  text: string;
}
export interface ProposedAction {
  id: string;
  type: string;
  params: { issue_type?: string; summary?: string; description?: string };
}

/** The draft's JSON columns (data model `drafts`), read defensively: they are `Any` in the API. */
export function draftParts(draft: Draft | null) {
  const evidence = (draft?.evidence ?? {}) as { chunks?: EvidenceChunk[]; account_facts?: AccountFact[] };
  return {
    citations: (draft?.citations ?? []) as unknown as Citation[],
    chunks: evidence.chunks ?? [],
    facts: evidence.account_facts ?? [],
    actions: (draft?.proposed_actions ?? []) as unknown as ProposedAction[],
    claims: ((draft?.unsupported_claims ?? []) as { claim?: string }[]).map((c) => c.claim ?? "").filter((c) => c !== ""),
  };
}

/** One ticket with every operator action. A `409 stale_draft` sets `stale` and reloads
 * (UI §3: "The customer wrote again"). */
export function useTicket(id: string) {
  const client = useQueryClient();
  const [stale, setStale] = useState(false);
  const key = ["console", "ticket", id] as const;
  const ticket = useQuery({
    queryKey: key,
    refetchInterval: 10_000,
    queryFn: async () => unwrap(await api.GET("/api/v1/console/tickets/{ticket_id}", { params: { path: { ticket_id: id } } })),
  });
  const version = () => ticket.data?.version ?? 0;

  // Every action: a stale draft shows the banner; the ticket reloads either way.
  const callbacks = {
    onError: (error: Error) => {
      if (error instanceof ApiError && error.code === "stale_draft") setStale(true);
      void client.invalidateQueries({ queryKey: key });
    },
    onSuccess: () => {
      setStale(false);
      void client.invalidateQueries({ queryKey: ["console"] });
    },
  };
  const path = { params: { path: { ticket_id: id } } };
  const versioned = () => ({ ...path, body: { ticket_version: version() } });
  const claim = useMutation({
    mutationFn: async () => unwrap(await api.POST("/api/v1/console/tickets/{ticket_id}/claim", versioned())),
    ...callbacks,
  });
  const release = useMutation({
    mutationFn: async () => unwrap(await api.POST("/api/v1/console/tickets/{ticket_id}/release", versioned())),
    ...callbacks,
  });
  const close = useMutation({
    mutationFn: async () => unwrap(await api.POST("/api/v1/console/tickets/{ticket_id}/close", versioned())),
    ...callbacks,
  });
  const reply = useMutation({
    mutationFn: async (body: string) =>
      unwrap(
        await api.POST("/api/v1/console/tickets/{ticket_id}/reply", {
          params: { path: { ticket_id: id }, header: { "Idempotency-Key": idempotencyKey() } },
          body: { ticket_version: version(), body },
        }),
      ),
    ...callbacks,
  });

  const draftPath = (draftId: string) => ({ params: { path: { draft_id: draftId }, header: { "Idempotency-Key": idempotencyKey() } } });
  const review = useMutation({
    mutationFn: async (
      input:
        | { kind: "approve"; draftId: string; actions: string[] }
        | { kind: "edit"; draftId: string; body: string; actions: string[] }
        | { kind: "reject"; draftId: string; reason: RejectReason; note: string; reply: string },
    ) => {
      if (input.kind === "approve") {
        return unwrap(await api.POST("/api/v1/console/drafts/{draft_id}/approve", { ...draftPath(input.draftId), body: { ticket_version: version(), approved_actions: input.actions } }));
      }
      if (input.kind === "edit") {
        return unwrap(
          await api.POST("/api/v1/console/drafts/{draft_id}/edit", { ...draftPath(input.draftId), body: { ticket_version: version(), body: input.body, approved_actions: input.actions } }),
        );
      }
      return unwrap(
        await api.POST("/api/v1/console/drafts/{draft_id}/reject", {
          ...draftPath(input.draftId),
          body: {
            ticket_version: version(),
            reason: input.reason,
            note: input.note.trim() === "" ? null : input.note.trim(),
            reply_body: input.reply.trim() === "" ? null : input.reply.trim(),
          },
        }),
      );
    },
    ...callbacks,
  });

  const createIssue = useMutation({
    mutationFn: async (params: { issue_type: "dispute" | "fraud" | "bug" | "limit_request"; summary: string; description: string }) =>
      unwrap(await api.POST("/api/v1/console/tickets/{ticket_id}/actions", { ...path, body: { type: "jira_create_issue", params } })),
    onSettled: () => void client.invalidateQueries({ queryKey: key }),
  });

  return { ticket, stale, dismissStale: () => { setStale(false); }, claim, release, close, reply, review, createIssue };
}
