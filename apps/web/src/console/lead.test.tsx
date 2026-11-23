import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { renderApp } from "../test/renderApp";

const LEAD = { id: "l1", email: "lena@tallybridge.test", name: "Lena", role: "lead" };
const SUMMARY = {
  period: { from: "2026-11-17", to: "2026-11-23" },
  tickets: { total: 7, auto_reply: 2, approval: 3, handoff: 1 },
  automation_share: 0.2857,
  ai_resolved_share: 0.1429,
  route_reasons: [{ reason: "low_confidence", count: 2 }],
  drafts: { approved: 1, edited: 2, rejected: 1, approved_unchanged_share: 0.25, median_edit_ratio: 0.3 },
  first_response_ms: { p50: 45000, p95: 58500 },
  latency_ms: { p50: 5500, p95: 9300 },
  cost_usd: { total: 0.0065, per_ticket: 0.000929, by_node: { triage: 0.001, write: 0.005 } },
  feedback: { up: 1, down: 1 },
  budget: { daily_limit_usd: 0.2, spent_today_usd: 0.004 },
};
const EVAL = {
  run_id: "2026-11-20-rag_hybrid-585ce6a",
  date: "2026-11-20",
  variant: "rag_hybrid",
  git_sha: "585ce6a",
  tickets_finished: 20,
  cost_usd: "0.019",
  checks: [
    { name: "hard-handoff recall", shown: "8 of 8", target_text: "1.00", red_line_text: "1.00", status: "pass", judged: false },
    { name: "citation validity", shown: "4 of 9", target_text: "0", red_line_text: "1", status: "red line missed", judged: true },
  ],
};
const SETTINGS = {
  auto_reply_enabled: false,
  confidence_threshold: 0.95,
  daily_budget_usd: 0.2,
  intent_allowlist: ["payment_pending", "withdrawal_issue"],
  allowed_intents: ["payment_pending", "withdrawal_issue", "fees_fx"],
};

function serve(extra: Record<string, (body: unknown) => Response> = {}) {
  const calls: { method: string; path: string; search: string; body: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      const text = await request.text();
      const body = text ? (JSON.parse(text) as unknown) : undefined;
      calls.push({ method: request.method, path: url.pathname, search: url.search, body });
      const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
      const own = extra[`${request.method} ${url.pathname}`];
      if (own) return own(body);
      switch (url.pathname) {
        case "/api/v1/auth/me":
          return json({ operator: LEAD, csrf_token: "csrf" });
        case "/api/v1/console/metrics/summary":
          return json(SUMMARY);
        case "/api/v1/console/metrics/timeseries":
          return json({ metric: url.searchParams.get("metric"), bucket: "day", period: SUMMARY.period, points: [{ date: "2026-11-23", values: { auto_reply: 2, approval: 3, handoff: 1, p50: 5500, p95: 9300 } }] });
        case "/api/v1/console/evals/latest":
          return json({ run: EVAL });
        case "/api/v1/admin/settings":
          return json(SETTINGS);
        case "/api/v1/console/audit":
          return json({ items: [{ id: 7, ts: "2026-11-23T10:00:00Z", actor_type: "operator", actor_id: "l1", action: "settings.changed", entity_type: "setting", entity_id: "auto_reply_enabled", details: { reason: "demo" } }], next_cursor: null });
        case "/api/v1/console/audit/verify":
          return json({ ok: true, checked: 7, broken_id: null });
        case "/api/v1/console/kb/search":
          return json({ items: [{ chunk_id: "c1", doc_id: "internal-fraud-checklist", title: "Fraud checklist", section: "Steps", url: "/help/internal-fraud-checklist#steps", visibility: "internal", text: "Check the device.", score: 0.8, rank: 1 }] });
        case "/api/v1/console/kb/documents/internal-fraud-checklist":
          return json({ id: "internal-fraud-checklist", title: "Fraud checklist", url: "", visibility: "internal", updated_at: "2026-11-01T00:00:00Z", sections: [{ anchor: "steps", heading: "Steps", text: "Check the device." }] });
        default:
          return json({ status: 404, code: "not_found", title: "", detail: "" }, 404);
      }
    }),
  );
  return calls;
}

describe("dashboard", () => {
  it("shows the numbers exactly as the API sends them, and the last evaluation", async () => {
    serve();
    renderApp("/console/dashboard");

    const cards = await screen.findByRole("region", { name: "Key numbers" });
    expect(within(cards).getByText("7")).toBeInTheDocument();
    expect(cards).toHaveTextContent("2 auto · 3 approval · 1 handoff");
    expect(cards).toHaveTextContent("28.6%");
    expect(cards).toHaveTextContent("AI-resolved 14.3%");
    expect(cards).toHaveTextContent("45.0 s");
    expect(cards).toHaveTextContent("$0.0009");
    expect(cards).toHaveTextContent("$0.0040 of $0.20");
    expect(screen.getByRole("figure", { name: "Route reasons" })).toHaveTextContent("Low confidence2");
    expect(screen.getByRole("figure", { name: "Draft outcomes" })).toHaveTextContent("2 (median edit 0.3)");
    const evaluation = screen.getByRole("region", { name: "Last evaluation" });
    expect(await within(evaluation).findByText(/585ce6a/)).toBeInTheDocument();
    expect(within(evaluation).getByText(/red line missed/)).toHaveTextContent("✗");
  });
});

describe("settings", () => {
  it("switching auto-reply on shows the last evaluation and needs a reason", async () => {
    const calls = serve({ "PUT /api/v1/admin/settings": (body) => new Response(JSON.stringify({ ...SETTINGS, auto_reply_enabled: (body as { auto_reply_enabled: boolean }).auto_reply_enabled }), { status: 200 }) });
    renderApp("/console/settings");

    await userEvent.click(await screen.findByRole("switch", { name: /Switch auto-reply on/ }));
    const dialog = screen.getByRole("dialog", { name: "Switch auto-reply on" });
    expect(await within(dialog).findByText("hard-handoff recall")).toBeInTheDocument();
    const confirm = within(dialog).getByRole("button", { name: "Confirm change" });
    expect(confirm).toBeDisabled();
    await userEvent.type(within(dialog).getByLabelText(/Reason/), "Demo day");
    await userEvent.click(confirm);

    await waitFor(() => {
      expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ auto_reply_enabled: true, reason: "Demo day" });
    });
    expect(await screen.findByRole("switch", { name: /Switch auto-reply off/ })).toHaveAttribute("aria-checked", "true");
  });

  it("removing an intent from the allow-list asks for a reason too", async () => {
    const calls = serve({ "PUT /api/v1/admin/settings": () => new Response(JSON.stringify(SETTINGS), { status: 200 }) });
    renderApp("/console/settings");
    await userEvent.click(await screen.findByRole("checkbox", { name: "withdrawal issue" }));
    const dialog = screen.getByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText(/Reason/), "Too many wrong answers");
    await userEvent.click(within(dialog).getByRole("button", { name: "Confirm change" }));
    await waitFor(() => {
      expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ intent_allowlist: ["payment_pending"], reason: "Too many wrong answers" });
    });
  });

  it("Escape closes the dialog without a change", async () => {
    const calls = serve();
    renderApp("/console/settings");
    await userEvent.click(await screen.findByRole("switch"));
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(calls.some((c) => c.method === "PUT")).toBe(false);
  });
});

describe("audit", () => {
  it("lists records, opens the JSON and verifies the chain", async () => {
    const calls = serve();
    renderApp("/console/audit");
    await screen.findByText("settings.changed");
    await userEvent.click(screen.getByRole("button", { name: /Details\s*of record 7/ }));
    expect(screen.getByRole("dialog")).toHaveTextContent('"reason": "demo"');
    await userEvent.click(screen.getByRole("button", { name: "Close" }));

    await userEvent.type(screen.getByLabelText("Action"), "settings.changed");
    await userEvent.click(screen.getByRole("button", { name: "Filter" }));
    await waitFor(() => {
      expect(calls.some((c) => c.search.includes("action=settings.changed"))).toBe(true);
    });
    await userEvent.click(screen.getByRole("button", { name: "Verify chain" }));
    expect(await screen.findByRole("status")).toHaveTextContent("OK: 7 records verified.");
  });
});

describe("knowledge base", () => {
  it("badges internal results and opens the article", async () => {
    serve();
    renderApp("/console/kb");
    await userEvent.type(await screen.findByLabelText("Search the knowledge base"), "fraud");
    await userEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText("Internal — never send to customers")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Fraud checklist" }));
    expect(await screen.findByRole("heading", { name: "Fraud checklist", level: 2 })).toBeInTheDocument();
  });
});
