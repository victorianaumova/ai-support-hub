# Web UI Specification

One React app with three areas: customer chat (demo), operator console, lead and compliance pages. Technology: React 19, TypeScript (strict), Vite, React Router, TanStack Query, Tailwind CSS. API types are generated from OpenAPI. No UI component library is required; the builder may use a small headless one (for dialogs, menus) if needed.

Design rules: clean and calm, works at 360 px and up; keyboard reachable; visible focus; colour is never the only signal; WCAG AA contrast. Dates are shown in the browser time zone. All text is English.

## 1. Routes

| Route | Area | Role |
|---|---|---|
| `/` | Landing: what the demo is, two buttons — "Try as a customer", "Open operator console" | public |
| `/chat` | Customer picker + chat | demo session |
| `/help/:docId` | Public help article (external KB documents) — the target of citation links | public |
| `/console/login` | Operator login | public |
| `/console` | Ticket queue | operator |
| `/console/tickets/:id` | Ticket workspace | operator |
| `/console/kb` | Knowledge-base search | operator |
| `/console/dashboard` | Metrics | lead |
| `/console/settings` | Auto-reply switch, threshold, budget, allow-list | lead |
| `/console/audit` | Audit log + chain check | compliance, lead |

## 2. Customer chat (`/chat`)

1. **Access and picker.** Field for the access code. Then a list of demo customers as cards: name, tier, a hint ("has a failed withdrawal", "card not delivered", "restricted account"). Explains that this replaces the login of the Tallybridge app.
2. **Chat.** Layout like an in-app support chat.
   - Top: AI notice (text from [security](security.md) §7), always visible; button **Talk to a human**.
   - Messages: customer on the right; AI and operators on the left with labels ("AI assistant", "Marta · Support team", "AI assistant · reviewed by Marta").
   - While a run is active: progress line from `run.status` events ("Understanding your question…", "Checking the help centre and your account…", "Writing the answer…", "Checking the answer…").
   - AI answer: text with citation markers `[1]`; under it "Sources" with links to `/help/:docId#section`. Buttons: helpful / not helpful (optional comment).
   - Handoff notice: a different style (info box) with the expected response time and the safe first step when present.
   - Masking: when the server masked something, a small note under the customer's message: "We hid a card number for your safety. We never need it in chat."
   - Input: max 4,000 characters, counter from 3,500; Enter sends, Shift+Enter new line; disabled while a run is active.
   - Suggested first messages (3–4 chips that fit the chosen customer).
3. **Earlier conversations** of this customer in a side list.

States to build: empty, sending, streaming progress, answered, waiting for a human (polling), error with retry, rate limited, budget stop ("Our assistant is not available now; a human will answer").

How they are reached (P10-02, `apps/web/src/chat/`): the stream's `run.status` drives the progress line; a stream with an `error` event (a budget stop or a failed run; both hand the ticket off with a notice) shows the budget-stop text; `ticket.status` `pending_approval` / `handed_off` shows a waiting line, and the chat polls `GET /tickets/{id}` and `/messages` every 3 s whenever no stream is open and the ticket is not closed, so an operator's reply appears; a stream that ends without `done` falls back to polling; `429` shows the `Retry-After` seconds; any other failure keeps the text and offers "Try again". The approval placeholder message is not stored and is replaced by the waiting line. The chosen customer's name and tier and the suggestions are kept in `sessionStorage` for the tab (the session itself is the HttpOnly cookie). "Talk to a human" is enabled once a ticket exists and is not already waiting.

**Safe rendering rule** (security T13, P8-04). Applies to every text the web app shows that a customer, an operator or a model wrote: messages, drafts, missing information, memory summaries, tool-call summaries, the operator summary, KB search hits.
- Text is rendered as text (React text nodes). Never `dangerouslySetInnerHTML`, never `innerHTML`, never an HTML string from the API.
- Markdown: only a small subset is turned into elements — paragraphs and line breaks, `**bold**`, `*italic*`, bulleted and numbered lists, `inline code`. Everything else (headings, tables, block quotes, raw HTML, HTML entities as markup) stays literal text. If a library parses it, raw HTML is turned off and the allowed node types are an explicit allow-list.
- No links from text: a URL written in a message, also as Markdown `[text](url)` or an autolink, is shown as plain text, not clickable. The only links are the "Sources" of a message, built from its `citations` (`url` is a relative `/help/<doc_id>#<anchor>` link built by the server; the customer API drops any other value to `""`, `hub.api.schemas.safe_citation_url`). A citation with an empty `url` (an account fact) has no link. The console's evidence and KB search links follow the same rule (relative `/help/...` only).
- No images, no iframes, no embedded media from any text.
- A link that opens in a new tab has `rel="noopener noreferrer"`.
- The Content-Security-Policy of the API (security §4: `script-src 'self'`, no inline script, no third-party origin) is the second line; the web app must work under it (no inline scripts or styles, no CDN).
- Tests (P10-05): a message holding `<img src=x onerror=…>`, `<script>`, `[x](javascript:…)` and a bare URL renders as visible text with no element, link or request.

## 3. Operator console

### Queue (`/console`)
- Tabs with counters: **Needs approval** (`pending_approval`), **Handed off** (`handed_off`), **Mine**, **Answered**, **All**.
- Row: urgency mark, customer name and tier, intent, first line of the summary, route reason chips, waiting time (turns red after the SLA from the world file: 30 min urgent, 4 business hours standard), assignee.
- Filters: intent, urgency, reason, search. Auto-refresh every 5 s (TanStack Query), new rows are marked.

### Ticket workspace (`/console/tickets/:id`)
Three columns on desktop, stacked on mobile.

**Left — conversation.** All messages with labels and times; masked parts shown as they are stored.

**Middle — work area.**
- *Why you see this ticket:* route, reason chips with plain explanations (for example "Low confidence: 0.71, threshold 0.85", "Policy: dispute tickets always need a human"), AI summary. For `policy_review` (row 6, `approval_only`) a "Reviewed because" list names the rules that matched: topic always reviewed (`intent`), high urgency, the draft proposes an action, repeat contact (`memory_shows_open_case`); a repeat contact links the earlier tickets on the same topic with intent, status and date (P12, `review` in the ticket detail).
- *Draft* (when present): editable text area with the AI text; citation markers are kept as chips; unsupported claims from the verify step are underlined with a tooltip; groundedness and confidence numbers.
- *Proposed actions:* for example a prepared Jira issue (type, summary, description) with a checkbox "Create on send".
- Buttons: **Approve and send** (disabled after any edit), **Send edited**, **Reject** (reason required: wrong facts / wrong tone / missing information / policy / other; optional own reply), **Claim / Release**, **Close ticket**.
- Free reply box for handed-off tickets.
- A 409 answer (stale draft) shows a banner "The customer wrote again" and reloads.

**Right — context.** Tabs:
- *Sources:* evidence chunks (title, section, score, text; the cited ones first and highlighted; a click on a citation chip scrolls here), account facts with the tool that returned them.
- *Customer:* profile, tier, limits used, restriction flag, cards, recent transactions. Limits, flag, cards and the newest 10 transactions are read when the tab opens, through the core read tools with the tool permissions (`GET /console/tickets/{id}/account`, node `console`; owner decision, PR #66). Data that could not be read is shown as "Unknown", never as "no" or "not flagged".
- *History:* memory summaries of earlier tickets, links to them.
- *Run:* steps with duration, tool calls, model, prompt versions, cost, link to the trace. Steps with duration come from `runs.steps` (P9-01, [ADR-0021](adr/0021-restore-run-tracing.md); owner decision on PR #66): every node in order with its duration, and its outcome when not `ok` (`error`, `timeout`, `paused`; a resumed run shows `human_review` twice). Below them the recorded model calls and tool calls with their latencies. The trace row links to Langfuse when the run was traced and `LANGFUSE_PROJECT_ID` is set, shows the bare trace id without a project id, and says "Not traced" otherwise.

Keyboard shortcuts: `A` approve, `E` focus the editor, `R` reject, `J`/`K` next/previous ticket.

As built (P10-03, `apps/web/src/console/`): shortcuts do nothing while focus is in a field; `J`/`K` follow the order of the queue tab the operator last opened (kept for the browser tab). Tab counters count the first page (50 rows; "50+" when there are more). The SLA mark uses the support hours of `config/policy.yaml` (Monday–Friday 08:00–20:00, Saturday 10:00–16:00, Europe/Bratislava), counted in 5-minute steps; urgent as the policy says (`support_hours`: urgency `high`, the reasons `fraud_report` and `restricted_account`, the intents `card_lost_stolen` and `account_restricted`), 30 minutes from a wait that starts in support hours and 60 outside them; `sla.test.ts` checks these values against `policy.yaml`. The draft editor is a text area; citation markers become chips in a preview under it (a click opens the source in the right column), and the unsupported claims of the verify step are underlined there.

### Knowledge-base search (`/console/kb`)
Search field, results with an `internal` / `external` badge, article view.

## 4. Lead pages

**Dashboard.** Period picker. Cards: tickets, automation share, median time to answer, cost per ticket, budget used today. Charts: routes per day (stacked), route reasons (bars), draft outcomes (approved unchanged / edited / rejected), latency p50/p95 per day, cost by node, feedback. A box "Last evaluation": date, variant, each metric with target, red line and pass/fail. A simple accessible chart library (for example Recharts) is fine. (As built, P10-04: no library; bars drawn with plain elements, each chart with its numbers as a table.)

**Settings.** Auto-reply switch (with a confirmation dialog that shows the last evaluation result), confidence threshold slider, daily budget, intent allow-list. The slider is shown disabled with the current threshold value read-only next to it and a note with the real state read from the settings (auto-reply on or off; the threshold is fixed), without counts from the last evaluation: the confidence threshold work (P9-03) was cut (ADR-0019; owner decision, PR #66), so the threshold has no working control. The cloud demo allows a lead to switch auto-reply on (ADR-0022); where the deployment does not, switching it on is refused (`409 auto_reply_not_allowed`, [API](api.md) `/admin/settings`). No risk–coverage curve, because there is no trained model (ADR-0018). Every change asks for a short reason, which goes to the audit log.

## 5. Audit page
Table with filters (entity, actor, action, time), a detail drawer with the JSON, and a "Verify chain" button that shows OK or the first broken record.

## 6. Tests

- Vitest + Testing Library for components with logic (message list, draft editor with citation chips, queue filters).
- Playwright e2e (fake models): customer asks a routine question and gets a cited answer; customer pastes a card number and sees it masked; fraud report → handoff notice → operator replies → customer sees it; operator approves a draft; operator edits and sends; stale draft conflict; lead switches auto-reply off and the next ticket goes to approval; role checks (operator cannot open settings).
- Accessibility check with axe in the e2e run on the main pages.

As built (P10-05, `apps/web/e2e/`): the scenarios run in order on one database with fake models. Only the SEPA ticket has recorded answers, so "routine question", "approve", "edit" and "stale draft" use it; the fraud report has no recording and is handed off with `system_error`, which still shows the handoff notice and exercises the operator reply. The safe-rendering case (`<img onerror>`, `<script>`, `[x](javascript:…)`, a bare URL) runs in the browser too. axe (WCAG 2.1 A/AA) checks the landing, chat picker, chat, help, login, queue, ticket workspace, dashboard, settings, audit and KB pages.
