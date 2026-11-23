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
});
