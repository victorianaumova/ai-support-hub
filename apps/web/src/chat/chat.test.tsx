import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderApp } from "../test/renderApp";
import { MAX_CHARS } from "./Composer";
import type { Message } from "./sse";

const PROFILE = { customer_ref: "CUS-0001", display_name: "Lucia Ferrandis", tier: "verified" };
const SEPA = "How long does a SEPA withdrawal take?";

function msg(id: string, role: Message["role"], body: string, extra: Partial<Message> = {}): Message {
  return {
    id,
    role,
    body,
    author_label: role === "customer" ? "You" : role === "system" ? "Tallybridge Support" : "AI assistant",
    is_ai_generated: role === "assistant",
    citations: [],
    masked: false,
    created_at: `2026-11-23T10:00:0${id.slice(-1)}Z`,
    ...extra,
  };
}

function sse(events: [string, unknown][], end = true): string {
  const blocks = events.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
  return blocks.join("") + (end ? "event: done\ndata: {}\n\n" : "");
}

interface Server {
  stream?: () => Response;
  messages: Message[];
  status: string;
  calls: { method: string; path: string; body: string }[];
}

function serve(server: Server) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const { pathname } = new URL(request.url);
      server.calls.push({ method: request.method, path: pathname, body: await request.text() });
      const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
        new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
      if (request.method === "POST" && (pathname === "/api/v1/tickets" || pathname.endsWith("/messages"))) {
        return server.stream ? server.stream() : new Response("", { status: 500 });
      }
      if (pathname === "/api/v1/tickets") return json({ items: [] });
      if (pathname.endsWith("/messages")) return json({ items: server.messages });
      if (pathname.endsWith("/request-human")) {
        server.status = "handed_off";
        return json({ id: "t1", status: "handed_off", ai_disclosure: "", ai_disclosed_at: "", created_at: "" });
      }
      if (pathname.endsWith("/feedback")) return json({ id: "f1" }, 201);
      if (pathname.startsWith("/api/v1/tickets/")) {
        return json({ id: "t1", status: server.status, ai_disclosure: "", ai_disclosed_at: "", created_at: "" });
      }
      if (pathname === "/api/v1/demo/customers") {
        return request.headers.get("X-Demo-Access-Code") === "good"
          ? json([{ ...PROFILE, hint: "SEPA withdrawal still on its way", suggestions: [SEPA] }])
          : json({ status: 401, code: "invalid_access_code", title: "", detail: "" }, 401);
      }
      if (pathname === "/api/v1/demo/session") return json(PROFILE);
      return json({ status: 404, code: "not_found", title: "", detail: "" }, 404);
    }),
  );
}

function streamOf(text: string): () => Response {
  return () => new Response(text, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

function loggedIn() {
  sessionStorage.setItem("hub.chat.profile", JSON.stringify(PROFILE));
  sessionStorage.setItem("hub.chat.suggestions", JSON.stringify([SEPA]));
}

beforeEach(() => {
  sessionStorage.clear();
});

describe("access and picker", () => {
  it("asks for the access code, then shows the customers as cards and opens the chat", async () => {
    const server: Server = { messages: [], status: "answered", calls: [] };
    serve(server);
    renderApp("/chat");

    await userEvent.type(screen.getByLabelText("Access code"), "bad");
    await userEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("This access code is not valid.");

    await userEvent.clear(screen.getByLabelText("Access code"));
    await userEvent.type(screen.getByLabelText("Access code"), "good");
    await userEvent.click(screen.getByRole("button", { name: "Continue" }));
    await userEvent.click(await screen.findByRole("button", { name: /Lucia Ferrandis/ }));

    expect(await screen.findByText(/You are chatting with Tallybridge's AI assistant/)).toBeInTheDocument();
    const session = server.calls.find((c) => c.path === "/api/v1/demo/session");
    expect(JSON.parse(session?.body ?? "{}")).toEqual({ customer_ref: "CUS-0001", access_code: "good" });
  });
});

describe("chat states (UI §2)", () => {
  it("empty: shows the suggested questions; streaming: shows the steps; answered: sources and feedback", async () => {
    loggedIn();
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const answer = msg("m2", "assistant", "It takes one business day [1].", {
      citations: [
        { marker: 1, title: "How long does a withdrawal take?", url: "/help/withdrawal-times#sepa-withdrawals" },
        { marker: 2, title: "Your account", url: "" },
      ],
    });
    const server: Server = {
      messages: [],
      status: "answered",
      calls: [],
      stream: () =>
        new Response(
          new ReadableStream({
            async start(controller) {
              const enc = new TextEncoder();
              controller.enqueue(enc.encode(sse([["ticket.created", { ticket_id: "t1", message: msg("m1", "customer", SEPA) }], ["run.status", { step: "searching" }]], false)));
              await gate;
              controller.enqueue(enc.encode(sse([["message", answer], ["ticket.status", { status: "answered" }]])));
              controller.close();
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        ),
    };
    serve(server);
    renderApp("/chat");

    await userEvent.click(await screen.findByRole("button", { name: SEPA }));
    expect(await screen.findByText("Checking the help centre and your account…")).toBeInTheDocument();
    expect(screen.getByLabelText("Your message")).toBeDisabled();

    server.messages = [msg("m1", "customer", SEPA), answer];
    act(() => {
      release();
    });
    const link = await screen.findByRole("link", { name: "How long does a withdrawal take?" });
    expect(link).toHaveAttribute("href", "/help/withdrawal-times#sepa-withdrawals");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    // An account fact has no link.
    expect(screen.getByText("Your account").closest("a")).toBeNull();
    expect(screen.getByLabelText("Your message")).toBeEnabled();

    await userEvent.click(screen.getByRole("button", { name: /Not helpful/ }));
    await userEvent.type(screen.getByPlaceholderText("What was missing? (optional)"), "too vague");
    await userEvent.click(screen.getByRole("button", { name: "Send feedback" }));
    expect(await screen.findByText("Thank you for your feedback.")).toBeInTheDocument();
    const feedback = server.calls.find((c) => c.path.endsWith("/feedback"));
    expect(JSON.parse(feedback?.body ?? "{}")).toEqual({ message_id: "m2", rating: "down", comment: "too vague" });
  });

  it("handoff: the notice is an info box; masking: a note under the customer's message; waiting: polling shows the reply", async () => {
    loggedIn();
    const customer = msg("m1", "customer", "My card [CARD ****1111] was stolen", { masked: true });
    const notice = msg("m2", "system", "I have passed your message to a support specialist.");
    const server: Server = {
      messages: [customer, notice],
      status: "handed_off",
      calls: [],
      stream: streamOf(sse([["ticket.created", { ticket_id: "t1", message: customer }], ["run.status", { step: "understanding" }], ["message", notice], ["ticket.status", { status: "handed_off" }]])),
    };
    serve(server);
    renderApp("/chat");

    await userEvent.type(screen.getByLabelText("Your message"), "My card 4111 1111 1111 1111 was stolen{Enter}");

    expect(await screen.findByRole("note")).toHaveTextContent("passed your message to a support specialist");
    expect(screen.getByText(/We hid a card number for your safety/)).toBeInTheDocument();
    expect(await screen.findByText(/Waiting for a support specialist/)).toBeInTheDocument();

    // The next poll (every 3 s) brings the operator's reply.
    server.messages = [customer, notice, msg("m3", "operator", "Your card is frozen now.", { author_label: "Marta · Support team" })];
    server.status = "answered";
    expect(await screen.findByText("Your card is frozen now.", {}, { timeout: 5_000 })).toBeInTheDocument();
    expect(screen.getByText("Marta · Support team")).toBeInTheDocument();
  });

  it("budget stop or failed run: says a human will answer", async () => {
    loggedIn();
    const customer = msg("m1", "customer", "Hello");
    const server: Server = {
      messages: [customer],
      status: "handed_off",
      calls: [],
      stream: streamOf(
        sse([
          ["ticket.created", { ticket_id: "t1", message: customer }],
          ["message", msg("m2", "system", "I could not prepare an answer, so I have passed your message to a support specialist.")],
          ["error", { status: 500, code: "internal_error", title: "", detail: "x" }],
          ["ticket.status", { status: "handed_off" }],
        ]),
      ),
    };
    serve(server);
    renderApp("/chat");
    await userEvent.type(screen.getByLabelText("Your message"), "Hello{Enter}");
    expect(await screen.findByText("Our assistant is not available now; a human will answer.")).toBeInTheDocument();
  });

  it("rate limited: tells when to try again", async () => {
    loggedIn();
    const server: Server = {
      messages: [],
      status: "answered",
      calls: [],
      stream: () =>
        new Response(JSON.stringify({ status: 429, code: "too_many_requests", title: "", detail: "" }), {
          status: 429,
          headers: { "Retry-After": "40" },
        }),
    };
    serve(server);
    renderApp("/chat");
    await userEvent.type(screen.getByLabelText("Your message"), "Hello{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("Please try again in 40 seconds.");
  });

  it("error with retry: a failed send can be sent again", async () => {
    loggedIn();
    const customer = msg("m1", "customer", "Hello");
    const server: Server = { messages: [], status: "answered", calls: [] };
    serve(server);
    renderApp("/chat");
    await userEvent.type(screen.getByLabelText("Your message"), "Hello{Enter}");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Your message could not be sent.");

    server.stream = streamOf(sse([["ticket.created", { ticket_id: "t1", message: customer }], ["ticket.status", { status: "answered" }]]));
    server.messages = [customer];
    await userEvent.click(within(alert).getByRole("button", { name: "Try again" }));
    await waitFor(() => {
      expect(server.calls.filter((c) => c.method === "POST" && c.path === "/api/v1/tickets")).toHaveLength(2);
    });
  });

  it("a broken stream falls back to polling", async () => {
    loggedIn();
    const customer = msg("m1", "customer", "Hello");
    const server: Server = {
      messages: [customer, msg("m2", "assistant", "Here is the answer.")],
      status: "answered",
      calls: [],
      stream: streamOf(sse([["ticket.created", { ticket_id: "t1", message: customer }], ["run.status", { step: "writing" }]], false)),
    };
    serve(server);
    renderApp("/chat");
    await userEvent.type(screen.getByLabelText("Your message"), "Hello{Enter}");
    expect(await screen.findByText("Here is the answer.")).toBeInTheDocument();
  });

  it("talk to a human asks for a handoff", async () => {
    loggedIn();
    const customer = msg("m1", "customer", "Hello");
    const server: Server = {
      messages: [customer],
      status: "answered",
      calls: [],
      stream: streamOf(sse([["ticket.created", { ticket_id: "t1", message: customer }], ["ticket.status", { status: "answered" }]])),
    };
    serve(server);
    renderApp("/chat");
    const human = screen.getByRole("button", { name: "Talk to a human" });
    expect(human).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Your message"), "Hello{Enter}");
    await waitFor(() => {
      expect(human).toBeEnabled();
    });
    await userEvent.click(human);
    await waitFor(() => {
      expect(server.calls.some((c) => c.path === "/api/v1/tickets/t1/request-human")).toBe(true);
    });
    expect(await screen.findByText(/Waiting for a support specialist/)).toBeInTheDocument();
  });
});

describe("composer", () => {
  it("Shift+Enter adds a line, Enter sends, and the counter starts at 3,500", async () => {
    loggedIn();
    serve({ messages: [], status: "answered", calls: [] });
    renderApp("/chat");
    const box = screen.getByLabelText("Your message");

    await userEvent.type(box, "line one{Shift>}{Enter}{/Shift}line two");
    expect(box).toHaveValue("line one\nline two");

    fireEvent.change(box, { target: { value: "x".repeat(3499) } });
    expect(screen.queryByText(/\/ 4000/)).toBeNull();
    fireEvent.change(box, { target: { value: "x".repeat(3500) } });
    expect(screen.getByText(`3500 / ${MAX_CHARS}`)).toBeInTheDocument();
    expect(box).toHaveAttribute("maxLength", String(MAX_CHARS));
  });
});
