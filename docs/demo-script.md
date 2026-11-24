# Demo script (P12-03)

A screen recording of the live demo with **no voice-over**: on-screen captions and music only (owner decision, 2026-11-24). Two cuts from the same takes:

- **Short cut, 60–90 s**, for the Upwork portfolio.
- **Long cut, about 3 min**: the same scenes, slower, plus the sources, the run trace and the MCP tools in Claude Code.

Recording is human gate H9 (owner). Captions are English, at most 8 words, and say only what the screen shows.

## Before recording

1. **Wake the demo** 2 minutes before: open <https://hub-api-5mytwmsq4q-ew.a.run.app/>. Cloud Run scales to zero, so the first request takes a few seconds.
2. **Two browser windows, side by side** (1440×900 screen, zoom 110%, bookmarks bar hidden). Use two browser profiles or one normal and one private window, so the chat and the console keep separate sign-ins.
   - Left, **customer**: `/chat`, enter the demo access code (owner's).
   - Right, **console**: `/console/login` as the lead **Noor Haddad**, `noor.haddad@tallybridge.example`, password = `SEED_OPERATOR_PASSWORD` (owner's). A lead can approve, open the dashboard and the settings, so one sign-in covers every scene.
3. **Settings → Auto-reply is on** (ADR-0023). Scene 6 switches it off; switch it back on after the take.
4. **Only five picker customers can be used.** The chat's picker (`data/seed/demo_customers.json`) shows six demo customers; CUS-0032 Thornbury Analytics Ltd is the deploy canary's customer ([operations](operations.md) §6) and CUS-0007 Jonas Albrecht is restricted (always handed off). The scenes below use the other customers and never ask one customer twice about the same topic.
5. **Check that each scene is fresh.** The repeat-contact rule (`memory_shows_open_case`) sends a ticket to approval when the same customer has another open ticket on the same topic, or a closed one from the last 14 days. An answered ticket stays open until `close-stale` closes it after 72 hours. So every take uses up its customer and topic for about two weeks. Before the take, in the console **All** tab, search each scene's customer: if they already have a ticket on that topic, use the backup (scene 1) or expect approval instead of an auto-reply.
6. **Rehearse locally, not on the live demo.** `make dev` with `LLM_MODE=real` in `.env` runs the same seed on your machine (a few cents of model calls); `docker compose -f infra/docker-compose.yml down -v` and `make dev` give a fresh database. On the live demo, record each scene once.
7. **Budget.** The demo's daily budget guard is $0.20. A full take costs a few cents (about one cent per ticket), so there is room for about 3–4 takes a day, canary included. Over the budget, every ticket is handed off with "Our assistant is not available now".
8. **Recorder:** macOS `Cmd+Shift+5`, "Record Selected Portion", both windows.

## Customers and messages

Type the messages exactly (the picker's suggestion chips are for visitors; several are used up by tests). The routes are from the routing rules ([AI pipeline](ai-pipeline.md) §6) and the dev-set run [`f3bfeb7`](../evals/runs/2026-11-24-rag_hybrid-f3bfeb7/report.md). **Fixed by** says whether code fixes the route or the model's answer can change it.

| Scene | Customer (topic) | Message | Route | Fixed by |
|---|---|---|---|---|
| 1 | CUS-0005 Priya Raghunathan (verification) | `My verification was rejected and the app shows ERR-4010. What does that mean?` | auto-reply | **varies**: needs confidence ≥ 0.95 and a clean verify step (see Fallbacks) |
| 1, backup | CUS-0016 Petra Horvathova (withdrawal) | `My 900 EUR withdrawal to my bank failed yesterday with ERR-2010. What does that mean?` | auto-reply | varies, as above |
| 2 | CUS-0001 Lucia Ferrandis (card limits) | `I want to pay my rent with my card 4242 4242 4242 4242. What is my daily card spending limit?` | auto-reply or approval | the **mask note is code** (G1); the route does not matter here |
| 3 | CUS-0010 Chloe Marchetti (fraud) | `My new card never arrived and now someone used my card 5555 5555 5555 4444 for two payments I did not make: 230 EUR at QuickCart and 80 EUR at a fuel station.` | handoff | **code**: a card number with fraud words (`pan_in_message_with_fraud_words`); the topic is a handoff topic too |
| 4 | CUS-0001 Lucia Ferrandis (dispute) | `A shop charged my card twice for one order, 189 EUR each, one minute apart. How do I dispute one of them?` | approval, reason Policy review | **code** once triage reads it as a dispute (`approval_only`); see Fallbacks |
| 6 | CUS-0005 Priya Raghunathan (fees) | `What does it cost to take cash out of an ATM with my card?` | approval, reason Auto-reply off | **code**: the switch is off |

The card numbers are public test numbers. Scene 6's ticket shows the reason Auto-reply off only on a fresh topic: a repeat contact is checked first and shows Policy review instead (still approval).

## Short cut (60–90 s)

Cut out the waiting: keep about one second of the progress line ("Checking the help centre and your account…"), then jump to the answer.

| # | What to click / type | Expected screen | Caption (≤ 8 words) | s |
|---|---|---|---|---|
| 0 | Title card (made in the editor) | "Tallybridge AI Support Hub" + "DEMO" pill; small line "Fictional company, synthetic data" | Tallybridge AI Support Hub · DEMO | 3 |
| 1 | Customer window: pick **Priya Raghunathan**, type the scene 1 message, Enter | Progress line, then the AI answer with `[1]` markers and **Sources** chips under it; label "AI assistant" | Routine question answered in seconds, with sources | 14 |
| 2 | Back to the picker, pick **Lucia Ferrandis**, type the scene 2 message | Under her message: "We hid a card number for your safety. We never need it in chat."; the number shows masked | Card numbers are hidden before any AI | 10 |
| 3 | Pick **Chloe Marchetti**, type the scene 3 message | Mask note; an info box (handoff notice): passed to a support specialist, "Please freeze your card in the app now…", "Tallybridge staff never ask for your full card number…", expected response time | Fraud goes straight to a human | 12 |
| 4 | Customer: pick **Lucia Ferrandis**, type the scene 4 message → waiting line. Console: **Needs approval** tab, open Lucia's dispute ticket | Middle column "Why you see this ticket": route approval, chip Policy review, "Reviewed because: topic always reviewed"; the draft with citation chips | Risky answers wait for one-click approval | 10 |
| 4b | Console: **Approve and send** (or `A`). Customer window | The answer appears in Lucia's chat, labelled "AI assistant · reviewed by Noor" | The customer gets the reviewed answer | 6 |
| 5 | Console sidebar: **Dashboard** | Five stat tiles (tickets, automation share, time to answer, cost per ticket, budget today), routes per day, route reasons, the "Last evaluation" box with pass/fail | Every route, cost and red line measured | 9 |
| 6 | Console: **Settings** → Auto-reply off → reason `demo: kill switch` → confirm. Customer: pick **Priya Raghunathan**, type the scene 6 message. Console: **Needs approval** | The new ticket waits; its reason chip is Auto-reply off | One switch sends every ticket to humans | 12 |
| 7 | End card (made in the editor) | "LangGraph · RAG · MCP · Guardrails · Evals" and the repository URL | Built with LangGraph, RAG, MCP and evals | 5 |
| | | | **Total** | **81** |

After the take: **Settings → Auto-reply on**, reason `demo end`.

## Long cut (about 3 min)

The same takes, with these scenes added (marked +). Keep the waiting a little longer: 2–3 seconds of the progress line.

| # | What to click / type | Expected screen | Caption (≤ 8 words) | s |
|---|---|---|---|---|
| 0 | Title card | as in the short cut | Tallybridge AI Support Hub · DEMO | 4 |
| 0+ | Landing page `/`, then the customer picker | Two buttons "Try as a customer", "Open operator console"; the picker cards with hints | A fictional payments company, synthetic customers | 8 |
| 1 | Scene 1 (Priya) | as in the short cut | Routine question answered in seconds, with sources | 16 |
| 1+ | Click a **Sources** chip | The public help article (`/help/...`) opens at the cited section | Every claim links to a help article | 8 |
| 2 | Scene 2 (Lucia) | as in the short cut | Card numbers are hidden before any AI | 10 |
| 3 | Scene 3 (Chloe) | as in the short cut | Fraud goes straight to a human | 12 |
| 3+ | Console: **Handed off** tab, open Chloe's ticket; type a short reply in the reply box, send; customer window | Reason chips (fraud report among them); the operator's reply appears in Chloe's chat with the operator's label | A specialist picks it up from here | 14 |
| 4 | Scene 4 (Lucia, dispute): open the ticket | as in the short cut | Risky answers wait for one-click approval | 10 |
| 4+ | Right column **Sources** tab, then **Customer** tab | Evidence chunks, cited ones first; account facts with the tool that returned them; profile, cards, recent transactions | Account data comes from read-only tools | 12 |
| 4b | **Approve and send**, customer window | as in the short cut | The customer gets the reviewed answer | 6 |
| 4+ | Right column **Run** tab | Every step with its duration, the model calls with tokens and cost, the prompt versions, the trace link; click **Open trace** (Langfuse) | Every step traced: time, model, cost | 14 |
| 5 | Dashboard | as in the short cut; scroll to the "Last evaluation" box | Every route, cost and red line measured | 12 |
| 5+ | Stay on the "Last evaluation" box | Two red lines marked missed | Missed red lines are shown, not hidden | 6 |
| 6 | Scene 6 (kill switch), as in the short cut | as in the short cut | One switch sends every ticket to humans | 14 |
| 7+ | Terminal in the repository with the MCP server added (README, "Connect the MCP server to Claude Code"). Run `claude`, type `/mcp` | `support-tools` connected, its tools listed (`core_get_customer`, `core_list_transactions`, `core_get_transaction`, `core_get_cards`, `jira_create_issue`, `slack_post_escalation`) | The same tools, as an open MCP server | 10 |
| 7+ | Type: `Use support-tools: show customer CUS-0016 and the last 5 transactions.` | Claude Code calls `core_get_customer` and `core_list_transactions`; Petra's profile and the failed 900 EUR withdrawal with ERR-2010 | Any MCP client can use them | 14 |
| 8 | End card | as in the short cut | Built with LangGraph, RAG, MCP and evals | 6 |
| | | | **Total** | **≈ 186** |

In the Hub the customer comes from the session, never from the model ([ADR-0011](adr/0011-mcp-tools-and-identity-binding.md)); Claude Code here is a developer client on mock data, so it names the customer itself. Do not show `jira_create_issue` or `slack_post_escalation` being called: in the Hub they run only after a human approves.

## Fallbacks

- **Scene 1 lands in Needs approval** (reason Low confidence or Unsupported claims): the model's answer varies. Retake with the backup (Petra). If that one goes to approval too, do not fake an auto-reply: record scene 1 as an approval, change its caption to `Not sure? A human checks first`, and keep scene 4 as it is.
- **Scene 4 lands in Handed off** (triage read it as a request for us to act): use any ticket in **Needs approval** (scene 1 or 2 may be there) for scenes 4 and 4b.
- **Scene 3** is fixed by code; if it is not handed off, stop recording: something is broken, check the deploy.
- **"Our assistant is not available now"**: the daily budget ran out. Record the next day.
- **First answer very slow**: cold start. Do a throwaway warm-up ticket with a rehearsal customer before the take.
- **Run tab shows "Not traced"**: skip the Open trace click; the steps and costs are still there.

## Editing (free tools)

- **CapCut** (desktop, free) or **iMovie**: cut the waits, add captions (one caption per scene, bottom centre, 2–3 s longer than it takes to read), title and end cards.
- **Music**: a calm instrumental track from the editor's free library or the YouTube Audio Library; check that the licence allows use in a portfolio.
- Export 1080p. Upwork: the short cut. The long cut: link it from the README.
