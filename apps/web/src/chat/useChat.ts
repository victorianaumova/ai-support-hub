import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, idempotencyKey, unwrap } from "../api/client";
import { ApiError } from "../api/problem";
import { type Message, postStream, type Step, StreamBrokenError, type StreamEvent, type TicketStatus } from "./sse";

/** How often the chat asks for new messages while no stream is open (api.md §2). */
export const POLL_MS = 3_000;

export type Phase =
  | { kind: "idle" }
  | { kind: "streaming"; step: Step | null }
  | { kind: "failed"; error: unknown; text: string }
  | { kind: "rate_limited"; retryAfterS: number | null; text: string };

export interface ChatState {
  ticketId: string | null;
  status: TicketStatus | null;
  /** Stored messages, plus the ones the open stream delivered and polling has not seen yet. */
  messages: Message[];
  phase: Phase;
  /** The last run failed or was stopped (budget): a human answers instead (UI §2). */
  assistantDown: boolean;
}

const TICKETS_KEY = ["customer", "tickets"] as const;

function merge(stored: Message[], live: Message[]): Message[] {
  const ids = new Set(stored.map((m) => m.id));
  const lastStored = stored.at(-1)?.created_at ?? "";
  // A live message the server did not store (the approval placeholder) is dropped once newer
  // stored messages exist; the status banner says what is happening instead.
  const extra = live.filter((m) => !ids.has(m.id) && m.created_at >= lastStored && m.role !== "system");
  return [...stored, ...extra];
}

export function useChat() {
  const client = useQueryClient();
  const [ticketId, setTicketId] = useState<string | null>(null);
  // The status the stream or an action reported, with when; polled data that is newer wins.
  const [liveStatus, setLiveStatusValue] = useState<{ value: TicketStatus | null; at: number }>({ value: null, at: 0 });
  const setStatus = useCallback((value: TicketStatus | null) => {
    setLiveStatusValue({ value, at: Date.now() });
  }, []);
  const [live, setLive] = useState<Message[]>([]);
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  const [assistantDown, setAssistantDown] = useState(false);
  const abort = useRef<AbortController | null>(null);

  useEffect(() => () => abort.current?.abort(), []);

  const streaming = phase.kind === "streaming";
  // Polling stops once the ticket is closed; the status of the last poll or event decides.
  const [closed, setClosed] = useState(false);
  const polling = ticketId !== null && !closed && !streaming;

  const stored = useQuery({
    queryKey: ["customer", "messages", ticketId],
    enabled: ticketId !== null && !streaming,
    refetchInterval: polling ? POLL_MS : false,
    queryFn: async () => {
      const id = ticketId ?? "";
      const [messages, ticket] = await Promise.all([
        api.GET("/api/v1/tickets/{ticket_id}/messages", { params: { path: { ticket_id: id } } }),
        api.GET("/api/v1/tickets/{ticket_id}", { params: { path: { ticket_id: id } } }),
      ]);
      const found = { messages: unwrap(messages).items, status: unwrap(ticket).status };
      setClosed(found.status === "closed");
      return found;
    },
  });

  const status: TicketStatus | null =
    stored.data !== undefined && stored.dataUpdatedAt > liveStatus.at ? stored.data.status : liveStatus.value;

  const tickets = useQuery({
    queryKey: TICKETS_KEY,
    queryFn: async () => unwrap(await api.GET("/api/v1/tickets")).items,
  });

  const handle = useCallback((event: StreamEvent) => {
    switch (event.event) {
      case "ticket.created":
        setTicketId(event.data.ticket_id);
        setLive((current) => [...current, event.data.message]);
        break;
      case "run.status":
        setPhase({ kind: "streaming", step: event.data.step });
        break;
      case "message":
        setLive((current) => [...current, event.data]);
        break;
      case "ticket.status":
        setStatus(event.data.status);
        setClosed(event.data.status === "closed");
        break;
      case "error":
        setAssistantDown(true);
        break;
      case "done":
        break;
    }
  }, [setStatus]);

  const send = useCallback(
    async (text: string) => {
      abort.current?.abort();
      const controller = new AbortController();
      abort.current = controller;
      setPhase({ kind: "streaming", step: null });
      setAssistantDown(false);
      const stream = (path: string) =>
        postStream({ path, body: { message: text }, idempotencyKey: idempotencyKey(), signal: controller.signal }, handle);
      const target = ticketId === null || status === "closed" ? null : ticketId;
      try {
        try {
          await stream(target === null ? "/api/v1/tickets" : `/api/v1/tickets/${target}/messages`);
        } catch (error) {
          if (!(error instanceof ApiError && error.code === "ticket_closed")) throw error;
          // An operator closed the ticket meanwhile: the message opens a new one (api.md §2).
          setTicketId(null);
          setStatus(null);
          setClosed(false);
          setLive([]);
          await stream("/api/v1/tickets");
        }
        setPhase({ kind: "idle" });
      } catch (error) {
        if (error instanceof StreamBrokenError) {
          // The run goes on without the stream; polling shows its answer (api.md §4).
          setPhase({ kind: "idle" });
        } else if (error instanceof ApiError && error.status === 429) {
          setPhase({ kind: "rate_limited", retryAfterS: error.retryAfterS, text });
        } else if (!controller.signal.aborted) {
          setPhase({ kind: "failed", error, text });
        }
      }
      void client.invalidateQueries({ queryKey: ["customer"] });
    },
    [client, handle, setStatus, status, ticketId],
  );

  const open = useCallback((id: string | null) => {
    abort.current?.abort();
    setTicketId(id);
    setStatus(null);
    setClosed(false);
    setLive([]);
    setPhase({ kind: "idle" });
    setAssistantDown(false);
  }, [setStatus]);

  const requestHuman = useCallback(async () => {
    if (ticketId === null) return;
    const ticket = unwrap(
      await api.POST("/api/v1/tickets/{ticket_id}/request-human", {
        params: { path: { ticket_id: ticketId }, header: { "Idempotency-Key": idempotencyKey() } },
      }),
    );
    setStatus(ticket.status);
    void client.invalidateQueries({ queryKey: ["customer"] });
  }, [client, setStatus, ticketId]);

  const state: ChatState = {
    ticketId,
    status,
    messages: merge(stored.data?.messages ?? [], live),
    phase,
    assistantDown,
  };
  return { state, send, open, requestHuman, tickets, pollError: stored.error };
}
