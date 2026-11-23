import { apiError, type ApiError, type Problem } from "../api/problem";
import type { components } from "../api/schema";

export type Message = components["schemas"]["MessageOut"];
export type TicketStatus = components["schemas"]["TicketOut"]["status"];
export type Step = "understanding" | "searching" | "writing" | "checking";

/**
 * Events of the run stream (api.md §4). The stream is not in the OpenAPI document (it is
 * `text/event-stream`), so only this envelope is written here; the message itself is the
 * generated `MessageOut`.
 */
export type StreamEvent =
  | { event: "ticket.created"; data: { ticket_id: string; message: Message } }
  | { event: "run.status"; data: { step: Step } }
  | { event: "message"; data: Message }
  | { event: "ticket.status"; data: { status: TicketStatus } }
  | { event: "error"; data: Problem }
  | { event: "done"; data: Record<string, never> };

const KNOWN = new Set(["ticket.created", "run.status", "message", "ticket.status", "error", "done"]);

/** Parses one SSE block (`event:` and `data:` lines). Comments (`: ping`) and unknown events
 * give `null`. */
export function parseBlock(block: string): StreamEvent | null {
  let event = "";
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    const value = colon === -1 ? "" : line.slice(colon + 1).replace(/^ /, "");
    if (field === "event") event = value;
    else if (field === "data") data.push(value);
  }
  if (!KNOWN.has(event) || data.length === 0) return null;
  try {
    return { event, data: JSON.parse(data.join("\n")) as unknown } as StreamEvent;
  } catch {
    return null;
  }
}

/** Thrown when the stream ended without `done` (network drop, proxy timeout). The caller falls
 * back to polling `GET /tickets/{id}/messages` (api.md §4). */
export class StreamBrokenError extends Error {
  constructor() {
    super("The answer stream was interrupted.");
    this.name = "StreamBrokenError";
  }
}

export interface StreamRequest {
  path: string;
  body: unknown;
  idempotencyKey: string;
  signal?: AbortSignal;
}

/**
 * POSTs a customer message and calls `onEvent` for each event (fetch + stream reader, not
 * `EventSource`, because it is a POST). Resolves after `done`. A non-2xx answer throws its
 * `ApiError` (e.g. 429 with `Retry-After`); a stream that stops early throws
 * `StreamBrokenError`.
 */
export async function postStream(request: StreamRequest, onEvent: (event: StreamEvent) => void): Promise<void> {
  const init: RequestInit = {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
      "Idempotency-Key": request.idempotencyKey,
    },
    body: JSON.stringify(request.body),
  };
  if (request.signal) init.signal = request.signal;
  const response = await globalThis.fetch(new Request(new URL(request.path, window.location.origin), init));
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null);
    throw apiError(response, body) satisfies ApiError;
  }
  if (response.body === null) throw new StreamBrokenError();
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  let done = false;
  try {
    for (;;) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += chunk.value.replace(/\r\n?/g, "\n");
      let end = buffer.indexOf("\n\n");
      while (end !== -1) {
        const event = parseBlock(buffer.slice(0, end));
        buffer = buffer.slice(end + 2);
        if (event) {
          onEvent(event);
          if (event.event === "done") done = true;
        }
        end = buffer.indexOf("\n\n");
      }
    }
  } catch {
    throw new StreamBrokenError();
  }
  if (!done) throw new StreamBrokenError();
}
