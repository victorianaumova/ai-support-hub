import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderApp } from "../test/renderApp";
import { unknownMarkers } from "./DraftEditor";

const OPERATOR = { id: "op1", email: "marta@tallybridge.test", name: "Marta", role: "operator" };
const CUSTOMER = { id: "c1", customer_ref: "CUS-0001", display_name: "Lucia Ferrandis", tier: "verified", country: "ES" };
const ROW = {
  id: "t1",
  status: "pending_approval",
  intent: "withdrawal_issue",
  urgency: "high",
  summary: "SEPA withdrawal not arrived.\nMore",
  last_route: "approval",
  route_reasons: ["auto_reply_off"],
  customer: CUSTOMER,
  assigned_operator: null,
  waiting_since: "2026-01-01T10:00:00Z",
  created_at: "2026-01-01T10:00:00Z",
  version: 3,
};
const DRAFT = {
  id: "d1",
  run_id: "r1",
  status: "pending",
  can_answer: true,
  missing_information: null,
  body: "A SEPA withdrawal takes one business day [1]. Fees are zero.",
  citations: [{ marker: 1, source_id: "ch1", doc_id: "withdrawal-times", title: "Withdrawal times", url: "/help/withdrawal-times#sepa" }],
  evidence: {
    chunks: [
      { chunk_id: "ch2", doc_id: "fees", title: "Fees", section: "Fees", url: "/help/fees#fees", score: 0.5, rank: 2, text: "Fee text" },
      { chunk_id: "ch1", doc_id: "withdrawal-times", title: "Withdrawal times", section: "SEPA", url: "/help/withdrawal-times#sepa", score: 0.9, rank: 1, text: "One business day." },
    ],
  },
  proposed_actions: [{ id: "a1", type: "jira_create_issue", params: { issue_type: "bug", summary: "Check transfer", description: "Look at TX" } }],
  groundedness: "0.500",
  unsupported_claims: [{ claim: "Fees are zero.", source_id: "ch1" }],
  reviewed_by: null,
  reviewed_at: null,
  final_body: null,
  edit_ratio: null,
  reject_reason: null,
  reject_note: null,
  approved_actions: [],
  created_at: "2026-01-01T10:00:05Z",
};
const TICKET = {
  ...ROW,
  language: "en",
  confidence: "0.910",
  ai_disclosed_at: ROW.created_at,
  first_response_at: null,
  resolved_at: null,
  resolution: null,
  messages: [
    { id: "m1", role: "customer", author_label: "Customer", is_ai_generated: false, author_operator_id: null, body: "Where is my money?", citations: [], masked_entities: {}, created_at: ROW.created_at },
  ],
  latest_run: null,
  draft: DRAFT,
  evidence: [],
  memory: [],
  tool_calls: [],
  trace_url: null,
};

interface Call {
  method: string;
  path: string;
  body: unknown;
  search: string;
}

function serve(overrides: Record<string, (call: Call) => Response> = {}): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      const text = await request.text();
      const call = { method: request.method, path: url.pathname, body: text ? (JSON.parse(text) as unknown) : undefined, search: url.search };
      calls.push(call);
      const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
      const own = overrides[`${call.method} ${call.path}`];
      if (own) return own(call);
      if (call.path === "/api/v1/auth/me") return json({ operator: OPERATOR, csrf_token: "csrf" });
      if (call.path === "/api/v1/console/tickets") {
        return json({ items: url.searchParams.getAll("status").includes("pending_approval") ? [ROW] : [], next_cursor: null });
      }
      if (call.path === "/api/v1/console/tickets/t1") return json(TICKET);
      if (call.path.startsWith("/api/v1/console/drafts/")) return json({ draft: DRAFT, message: null, ticket: ROW });
      return json({ status: 404, code: "not_found", title: "", detail: "" }, 404);
    }),
  );
  return calls;
}

beforeEach(() => {
  sessionStorage.clear();
});

describe("queue", () => {
  it("shows the rows of the tab with counters, reasons and the SLA mark", async () => {
    const calls = serve();
    renderApp("/console");

    const link = await screen.findByRole("link", { name: "Lucia Ferrandis" });
    expect(link).toHaveAttribute("href", "/console/tickets/t1");
    const row = link.closest("tr");
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByText("⚑ Urgent")).toBeInTheDocument();
    expect(within(row as HTMLElement).getByText("over SLA")).toBeInTheDocument();
    expect(within(row as HTMLElement).getByText("SEPA withdrawal not arrived.")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Needs approval/ })).toHaveTextContent("1");

    await userEvent.selectOptions(screen.getByLabelText("Intent"), "withdrawal_issue");
    await waitFor(() => {
      expect(calls.some((c) => c.search.includes("intent=withdrawal_issue"))).toBe(true);
    });
    await userEvent.click(screen.getByRole("tab", { name: /Mine/ }));
    await waitFor(() => {
      expect(calls.some((c) => c.search.includes("assigned=me"))).toBe(true);
    });
  });
});

describe("ticket workspace", () => {
  it("approves the draft unchanged with the ticked action", async () => {
    const calls = serve();
    renderApp("/console/tickets/t1");

    await screen.findByRole("heading", { name: "AI draft" });
    expect(screen.getByText("Auto-reply is switched off")).toBeInTheDocument();
    // The unsupported claim is underlined in the preview.
    expect(screen.getByTitle(/no support for this claim/).textContent).toBe("Fees are zero.");
    await userEvent.click(screen.getByRole("checkbox"));
    await userEvent.click(screen.getByRole("button", { name: "Approve and send" }));

    await waitFor(() => {
      expect(calls.find((c) => c.path === "/api/v1/console/drafts/d1/approve")?.body).toEqual({ ticket_version: 3, approved_actions: ["a1"] });
    });
  });

  it("turns off Approve after an edit and sends the edited text", async () => {
    const calls = serve();
    renderApp("/console/tickets/t1");
    const box = await screen.findByLabelText(/Answer to the customer/);

    await userEvent.type(box, " Thanks.");
    expect(screen.getByRole("button", { name: "Approve and send" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Send edited" }));

    await waitFor(() => {
      expect(calls.find((c) => c.path === "/api/v1/console/drafts/d1/edit")?.body).toEqual({
        ticket_version: 3,
        body: `${DRAFT.body} Thanks.`,
        approved_actions: [],
      });
    });
  });

  it("refuses to send a marker without a source", async () => {
    serve();
    renderApp("/console/tickets/t1");
    const box = await screen.findByLabelText(/Answer to the customer/);
    fireEvent.change(box, { target: { value: "See [1] and [7]." } });
    expect(screen.getByRole("alert")).toHaveTextContent("No source for marker [7]");
    expect(screen.getByRole("button", { name: "Send edited" })).toBeDisabled();
    expect(unknownMarkers("[1] [2] [2]", [{ marker: 1 }])).toEqual([2]);
  });

  it("a citation chip opens its source, cited first", async () => {
    serve();
    renderApp("/console/tickets/t1");
    await userEvent.click(await screen.findByRole("button", { name: "Source 1" }));
    const panel = screen.getByRole("tabpanel");
    const titles = within(panel).getAllByRole("link").map((a) => a.textContent);
    expect(titles).toEqual(["Withdrawal times", "Fees"]);
    expect(document.getElementById("source-1")).toHaveClass("ring-2");
  });

  it("reject needs a reason", async () => {
    const calls = serve();
    renderApp("/console/tickets/t1");
    await userEvent.click(await screen.findByRole("button", { name: "Reject" }));
    const submit = screen.getByRole("button", { name: "Reject draft" });
    expect(submit).toBeDisabled();
    await userEvent.selectOptions(screen.getByLabelText(/Reason/), "wrong_facts");
    await userEvent.type(screen.getByLabelText(/Note for the team/), "fee is wrong");
    await userEvent.click(submit);
    await waitFor(() => {
      expect(calls.find((c) => c.path === "/api/v1/console/drafts/d1/reject")?.body).toEqual({
        ticket_version: 3,
        reason: "wrong_facts",
        note: "fee is wrong",
        reply_body: null,
      });
    });
  });

  it("shows the conflict banner when the customer wrote again (409 stale_draft)", async () => {
    let loads = 0;
    serve({
      "POST /api/v1/console/drafts/d1/approve": () =>
        new Response(JSON.stringify({ status: 409, code: "stale_draft", title: "Conflict", detail: "" }), { status: 409 }),
      "GET /api/v1/console/tickets/t1": () => {
        loads += 1;
        return new Response(JSON.stringify(TICKET), { status: 200 });
      },
    });
    renderApp("/console/tickets/t1");
    await userEvent.click(await screen.findByRole("button", { name: "Approve and send" }));

    expect(await screen.findByText("The customer wrote again.")).toBeInTheDocument();
    await waitFor(() => {
      expect(loads).toBeGreaterThan(1);
    });
  });

  it("keyboard: A approves, R opens reject, E focuses the editor, J goes to the next ticket", async () => {
    sessionStorage.setItem("hub.console.queue-order", JSON.stringify(["t1", "t2"]));
    const calls = serve();
    const router = renderApp("/console/tickets/t1");
    await screen.findByRole("heading", { name: "AI draft" });

    await userEvent.keyboard("e");
    expect(screen.getByLabelText(/Answer to the customer/)).toHaveFocus();
    // Typed into the editor, a letter is text, not a shortcut.
    await userEvent.keyboard("a");
    expect(calls.some((c) => c.path.endsWith("/approve"))).toBe(false);
    (document.activeElement as HTMLElement).blur();

    await userEvent.keyboard("r");
    expect(screen.getByRole("button", { name: "Reject draft" })).toBeInTheDocument();
    await userEvent.keyboard("j");
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/console/tickets/t2");
    });
  });

  it("keyboard: A outside a field approves with the ticked actions, once", async () => {
    let release: (value: Response) => void = () => undefined;
    const calls = serve({
      // The approve hangs, so the second press comes while the first is still on its way.
      "POST /api/v1/console/drafts/d1/approve": () => new Response(JSON.stringify({ draft: DRAFT, message: null, ticket: ROW })),
    });
    const fetchMock = vi.mocked(fetch);
    const real = fetchMock.getMockImplementation();
    fetchMock.mockImplementation(async (input) => {
      const request = input as Request;
      if (request.url.endsWith("/approve")) {
        await new Promise<Response>((resolve) => {
          release = resolve;
        });
      }
      return (real as (r: Request) => Promise<Response>)(request);
    });
    renderApp("/console/tickets/t1");
    await screen.findByRole("heading", { name: "AI draft" });
    await userEvent.click(screen.getByRole("checkbox"));
    (document.activeElement as HTMLElement).blur();

    await userEvent.keyboard("a");
    await userEvent.keyboard("a");
    release(new Response());

    await waitFor(() => {
      expect(calls.filter((c) => c.path === "/api/v1/console/drafts/d1/approve")).toHaveLength(1);
    });
    expect(calls.find((c) => c.path === "/api/v1/console/drafts/d1/approve")?.body).toEqual({ ticket_version: 3, approved_actions: ["a1"] });
  });

  it("keyboard: A after an edit does nothing (Send edited is the way)", async () => {
    const calls = serve();
    renderApp("/console/tickets/t1");
    const box = await screen.findByLabelText(/Answer to the customer/);
    await userEvent.type(box, " Thanks.");
    box.blur();

    await userEvent.keyboard("a");

    expect(screen.getByRole("button", { name: "Approve and send" })).toBeDisabled();
    expect(calls.some((c) => c.path.endsWith("/approve"))).toBe(false);
  });

  it("an internal source is plain text, not a help link", async () => {
    const internal = { chunk_id: "ch9", doc_id: "ops-manual", title: "Ops manual", section: "Holds", url: "/help/ops-manual#holds", visibility: "internal", score: 0.4, rank: 3, text: "Internal text" };
    serve({
      "GET /api/v1/console/tickets/t1": () =>
        new Response(JSON.stringify({ ...TICKET, draft: { ...DRAFT, evidence: { chunks: [...DRAFT.evidence.chunks, internal] } } })),
    });
    renderApp("/console/tickets/t1");
    await screen.findByRole("heading", { name: "AI draft" });
    const panel = screen.getByRole("tabpanel");
    expect(within(panel).getByText("Ops manual").closest("a")).toBeNull();
    expect(within(panel).getByRole("link", { name: "Fees" })).toHaveAttribute("href", "/help/fees#fees");
  });

  it("says which approval rule matched and links the ticket a repeat contact repeats", async () => {
    const review = {
      rules: ["high_urgency", "memory_shows_open_case"],
      repeat_of: [{ id: "t0", intent: "withdrawal_issue", status: "answered", created_at: ROW.created_at }],
    };
    serve({ "GET /api/v1/console/tickets/t1": () => new Response(JSON.stringify({ ...TICKET, route_reasons: ["policy_review"], review })) });
    renderApp("/console/tickets/t1");
    const why = (await screen.findByRole("heading", { name: "Why you see this ticket" })).closest("section") as HTMLElement;
    expect(within(why).getByText("Policy: drafts of this kind are reviewed")).toBeInTheDocument();
    expect(within(why).getByText("High urgency")).toBeInTheDocument();
    expect(within(why).getByText("Repeat contact: the customer wrote about this before")).toBeInTheDocument();
    expect(within(why).getByRole("link", { name: /withdrawal issue · answered/ })).toHaveAttribute("href", "/console/tickets/t0");
  });

  it("the Run tab lists the recorded model calls with their latency", async () => {
    const call = { id: "l1", node: "triage", model: "gpt-x", prompt_version: "triage/v1", status: "ok", latency_ms: 812, cost_usd: "0.000400", input_tokens: 500, output_tokens: 50, created_at: ROW.created_at };
    serve({ "GET /api/v1/console/tickets/t1": () => new Response(JSON.stringify({ ...TICKET, model_calls: [call] })) });
    renderApp("/console/tickets/t1");
    await userEvent.click(await screen.findByRole("tab", { name: "Run" }));
    expect(screen.getByText(/triage\/v1 · ok · 812 ms/)).toBeInTheDocument();
  });

  it("the Run tab shows each step with its duration and links the trace", async () => {
    const run = {
      id: "r1", status: "interrupted", route: "approval", route_reasons: [], confidence: null, prompt_versions: {}, models: {},
      total_cost_usd: "0", input_tokens: 0, output_tokens: 0, latency_ms: null, error: null, trace_id: "ab12", started_at: ROW.created_at, finished_at: null,
      steps: [
        { node: "triage", started_at: ROW.created_at, duration_ms: 1840, outcome: "ok" },
        { node: "verify", started_at: ROW.created_at, duration_ms: 110000, outcome: "timeout" },
        { node: "human_review", started_at: ROW.created_at, duration_ms: 35, outcome: "paused" },
      ],
    };
    serve({ "GET /api/v1/console/tickets/t1": () => new Response(JSON.stringify({ ...TICKET, latest_run: run, trace_url: "https://cloud.langfuse.com/project/p/traces/ab12" })) });
    renderApp("/console/tickets/t1");
    await userEvent.click(await screen.findByRole("tab", { name: "Run" }));
    expect(screen.getByText("triage · 1840 ms")).toBeInTheDocument();
    expect(screen.getByText("verify · 110000 ms · timeout")).toBeInTheDocument();
    expect(screen.getByText("human_review · 35 ms · paused")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open in Langfuse" })).toHaveAttribute("href", "https://cloud.langfuse.com/project/p/traces/ab12");
  });

  it("the Run tab says when a run was not traced", async () => {
    const run = {
      id: "r1", status: "completed", route: "handoff", route_reasons: [], confidence: null, prompt_versions: {}, models: {},
      total_cost_usd: "0", input_tokens: 0, output_tokens: 0, latency_ms: 10, error: null, trace_id: null, steps: [], started_at: ROW.created_at, finished_at: ROW.created_at,
    };
    serve({ "GET /api/v1/console/tickets/t1": () => new Response(JSON.stringify({ ...TICKET, latest_run: run })) });
    renderApp("/console/tickets/t1");
    await userEvent.click(await screen.findByRole("tab", { name: "Run" }));
    expect(screen.getByText("Not traced")).toBeInTheDocument();
    expect(screen.getByText("No steps recorded.")).toBeInTheDocument();
  });
});

const ACCOUNT = {
  profile: {
    tier: "verified",
    country: "ES",
    restricted: false,
    limits: [{ name: "withdraw_per_day", limit_eur: "5000.00", used: { EUR: "250.00" } }],
    as_of: "2026-11-12",
  },
  cards: [{ card_ref: "CRD-0001", kind: "physical", last4: "2018", status: "active", delivery_status: "delivered", expected_delivery_by: null, delivery_overdue: false }],
  transactions: [{ reference: "TX-000001", type: "sepa_withdrawal", amount: "250.00", currency: "EUR", status: "pending", error_code: null, requested_at: "2026-11-10T09:00:00Z" }],
  unavailable: [],
};

describe("customer tab", () => {
  it("shows limits used, the restricted flag, cards and recent transactions, read once", async () => {
    const calls = serve({ "GET /api/v1/console/tickets/t1/account": () => new Response(JSON.stringify(ACCOUNT)) });
    renderApp("/console/tickets/t1");
    await userEvent.click(await screen.findByRole("tab", { name: "Customer" }));
    const panel = screen.getByRole("tabpanel");

    expect(await within(panel).findByText(/withdraw per day/i)).toBeInTheDocument();
    expect(within(panel).getByText("Restricted").nextElementSibling).toHaveTextContent("No");
    expect(within(panel).getByText(/•••• 2018/)).toBeInTheDocument();
    expect(within(panel).getByText("TX-000001")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Sources" }));
    await userEvent.click(screen.getByRole("tab", { name: "Customer" }));
    expect(calls.filter((c) => c.path.endsWith("/account"))).toHaveLength(1);
  });

  it("says Unknown, never No, when the profile could not be read", async () => {
    serve({
      "GET /api/v1/console/tickets/t1/account": () =>
        new Response(JSON.stringify({ ...ACCOUNT, profile: null, unavailable: ["profile"] })),
    });
    renderApp("/console/tickets/t1");
    await userEvent.click(await screen.findByRole("tab", { name: "Customer" }));
    const panel = screen.getByRole("tabpanel");

    expect(await within(panel).findByText(/the limits could not be read/)).toBeInTheDocument();
    expect(within(panel).getByText("Restricted").nextElementSibling).toHaveTextContent("Unknown");
    expect(within(panel).queryByText("Not flagged by this run")).toBeNull();
  });

  it("says Unknown when the account API fails", async () => {
    serve({
      "GET /api/v1/console/tickets/t1/account": () =>
        new Response(JSON.stringify({ status: 503, code: "tools_unavailable", title: "", detail: "" }), { status: 503 }),
    });
    renderApp("/console/tickets/t1");
    await userEvent.click(await screen.findByRole("tab", { name: "Customer" }));
    const panel = screen.getByRole("tabpanel");

    expect(await within(panel).findByRole("alert")).toHaveTextContent("unknown");
    expect(within(panel).getByText("Restricted").nextElementSibling).toHaveTextContent("Unknown");
  });
});
