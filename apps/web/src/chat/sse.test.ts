import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/problem";
import { parseBlock, postStream, type StreamEvent, StreamBrokenError } from "./sse";

function streamResponse(chunks: string[], status = 200): Response {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(new TextEncoder().encode(chunk));
      controller.close();
    },
  });
  return new Response(body, { status, headers: { "Content-Type": "text/event-stream" } });
}

describe("parseBlock", () => {
  it("reads event and data, ignores comments and unknown events", () => {
    expect(parseBlock('event: run.status\ndata: {"step":"writing"}')).toEqual({ event: "run.status", data: { step: "writing" } });
    expect(parseBlock(": ping")).toBeNull();
    expect(parseBlock('event: secret\ndata: {}')).toBeNull();
    expect(parseBlock("event: done\ndata: not json")).toBeNull();
  });
});

describe("postStream", () => {
  it("delivers events across chunk borders and sends the idempotency key", async () => {
    const fetch = vi.fn<(request: Request) => Promise<Response>>(() =>
      Promise.resolve(
        streamResponse([
          'event: ticket.created\ndata: {"ticket_id":"t1","message":{}}\n\n: ping\n\nevent: run.st',
          'atus\ndata: {"step":"understanding"}\n\nevent: ticket.status\ndata: {"status":"handed_off"}\n\nevent: done\ndata: {}\n\n',
        ]),
      ),
    );
    vi.stubGlobal("fetch", fetch);
    const events: StreamEvent[] = [];

    await postStream({ path: "/api/v1/tickets", body: { message: "hi" }, idempotencyKey: "k1" }, (e) => events.push(e));

    expect(events.map((e) => e.event)).toEqual(["ticket.created", "run.status", "ticket.status", "done"]);
    const request = fetch.mock.calls[0]?.[0];
    expect(request?.headers.get("Idempotency-Key")).toBe("k1");
    expect(request?.method).toBe("POST");
  });

  it("throws StreamBrokenError when the stream stops before done", async () => {
    vi.stubGlobal("fetch", () => Promise.resolve(streamResponse(['event: run.status\ndata: {"step":"writing"}\n\n'])));
    await expect(postStream({ path: "/x", body: {}, idempotencyKey: "k" }, () => undefined)).rejects.toBeInstanceOf(StreamBrokenError);
  });

  it("throws the ApiError of a refused request with Retry-After", async () => {
    vi.stubGlobal("fetch", () =>
      Promise.resolve(
        new Response(JSON.stringify({ status: 429, code: "too_many_requests", title: "", detail: "slow" }), {
          status: 429,
          headers: { "Retry-After": "42" },
        }),
      ),
    );
    const error: unknown = await postStream({ path: "/x", body: {}, idempotencyKey: "k" }, () => undefined).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).retryAfterS).toBe(42);
  });
});
