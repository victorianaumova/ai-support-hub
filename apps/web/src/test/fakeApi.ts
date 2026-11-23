import { vi } from "vitest";

export interface Call {
  method: string;
  path: string;
  headers: Headers;
  body: unknown;
}

type Handler = (call: Call) => { status: number; body?: unknown; headers?: Record<string, string> };

/** Replaces `fetch` with handlers keyed by "METHOD /path"; records every call. */
export function fakeApi(handlers: Record<string, Handler>): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: Request) => {
      const url = new URL(input.url);
      const text = await input.text();
      const call: Call = {
        method: input.method,
        path: url.pathname,
        headers: input.headers,
        body: text === "" ? undefined : (JSON.parse(text) as unknown),
      };
      calls.push(call);
      const handler = handlers[`${call.method} ${call.path}`];
      const answer = handler ? handler(call) : { status: 404, body: { status: 404, code: "not_found", title: "Not Found", detail: "" } };
      const body = answer.body === undefined ? null : JSON.stringify(answer.body);
      return new Response(body, {
        status: answer.status,
        headers: { "Content-Type": "application/json", "X-Request-ID": "testrequestid", ...answer.headers },
      });
    }),
  );
  return calls;
}

export const OPERATOR = { id: "0193-op", email: "marta@tallybridge.test", name: "Marta", role: "operator" } as const;
export const LEAD = { ...OPERATOR, name: "Lena", email: "lena@tallybridge.test", role: "lead" } as const;
