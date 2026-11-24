# Product Requirements — Enterprise AI Support Hub

Status: approved for build · Owner: Viktoriia Naumova · Related: [design](design.md), [eval plan](eval-plan.md), [Tallybridge world](tallybridge-world.md)

## 1. Problem

Tallybridge (a fictional payments company, see [the world](tallybridge-world.md)) gets about 1,200 support tickets per day. The median first response is 3 h 40 min. Most tickets are about a small number of topics, and the answer is already in the knowledge base or in the customer's own transaction data. Operators spend their time on copy-paste answers, and customers with urgent problems (fraud, blocked money) wait in the same queue.

A wrong answer about money is expensive: the customer loses trust, and the company can break a legal rule. So the goal is **not** "answer as many tickets as possible with AI". The goal is: answer the simple tickets fast and correctly, and send everything else to a human with good preparation.

## 2. Goals and non-goals

**Goals**
1. Answer routine tickets in seconds, with a link to the source for every fact.
2. Know when not to answer: send risky, unclear or unsupported tickets to an operator.
3. Make the operator faster: a ready draft, the sources, the customer's data and a summary in one screen.
4. Give the support lead numbers: quality, automation share, cost.
5. Protect card data and personal data, and keep a record of every AI and human action.

**Non-goals**
- No money movement by AI (refunds, fee returns, limit changes, unfreezing accounts).
- No voice, no email channel, no languages other than English in the MVP.
- No integration with an existing helpdesk product (Zendesk, Intercom). A real company would connect the Hub to its helpdesk. Here the Hub has its own small ticket store and console, so that the demo is complete.
- No training or fine-tuning of models (the offline fine-tuning experiment was removed by ADR-0018).
- Not a real PCI DSS or GDPR certification. The system follows the main ideas of these rules.

## 3. Users

| User | Job to be done | Pain today |
|---|---|---|
| **Customer** (freelancer, small business) | "When my money is late or my card fails, tell me what is happening and what to do — now." | Waits hours for a simple answer. Does not know if a person or a bot answers. |
| **Support operator** | "Help me close tickets correctly and fast, and never make me send a wrong answer about money." | Copy-paste answers, search in several tools, no time for hard cases. |
| **Support lead** (supervisor) | "Show me that AI answers are safe, how many tickets it closes, and what it costs." | No numbers on quality. Afraid of a "Klarna reverse": cheap but bad. |
| **Compliance / security officer** | "Prove that card data is not leaked and that every action has an owner." | New AI system = new risk; needs audit records. |

## 4. User journeys

**Customer, routine question.** Opens the in-app chat → sees a notice that an AI assistant answers first → writes "My withdrawal from Monday has not arrived" → in about 10 seconds gets an answer: the status of that withdrawal, the normal timeframe, the next step, links to help articles → marks the answer as helpful, or asks for a human.

**Customer, risky case.** Writes "There is a card payment I did not make" → at once gets the safe first step (freeze the card in the app) and a clear message that a human specialist takes the case, with the expected response time → the operator replies in the same chat.

**Operator.** Opens the console → queue sorted by urgency → opens a ticket → sees the conversation, the AI summary, the draft answer with highlighted sources, the customer's account facts, the reason why the AI did not answer alone → approves, edits or rejects the draft → the answer goes to the customer; the action is recorded.

**Support lead.** Opens the dashboard → sees automation share, wrong-answer rate from the last evaluation, escalation reasons, cost per ticket, latency → can switch auto-answers off with one switch.

## 5. User stories

IDs are used in the build plan and in tests.

**Customer**
- US-C1. As a customer, I want an answer to a routine question within seconds, so that I do not wait hours.
- US-C2. As a customer, I want to know that I talk to an AI and which messages a human wrote, so that I can judge the answer.
- US-C3. As a customer, I want the answer to use my real transaction or card status, so that it is about my case and not a general text.
- US-C4. As a customer, I want links to the help articles behind the answer, so that I can check it.
- US-C5. As a customer, I want to ask for a human at any time, so that I am not stuck with a bot.
- US-C6. As a customer, I want to rate the answer, so that bad answers get noticed.
- US-C7. As a customer who pasted a card number by mistake, I want it hidden, so that it is not stored or shown.

**Operator**
- US-O1. As an operator, I want a queue of tickets that need me, sorted by urgency, so that I take the most important first.
- US-O2. As an operator, I want to see which articles and account facts the draft uses, so that I can check it in one minute.
- US-O3. As an operator, I want to approve, edit or reject a draft, so that I control what the customer gets.
- US-O4. As an operator, I want to see why the AI did not answer alone, so that I know what to look at.
- US-O5. As an operator, I want a short summary of the ticket and of the customer's earlier tickets, so that I do not read everything again.
- US-O6. As an operator, I want a Jira issue prepared for disputes and fraud cases, so that I do not type the data again.
- US-O7. As an operator, I want to search the knowledge base, including internal procedures, so that I can answer hard cases.

**Support lead**
- US-L1. As a lead, I want a dashboard with automation share, escalation reasons, cost and latency, so that I can manage the service.
- US-L2. As a lead, I want to set the confidence threshold and to switch auto-answers off, so that I control the risk.
- US-L3. As a lead, I want to see how often operators change AI drafts, so that I know where the AI is weak.

**Compliance**
- US-S1. As a compliance officer, I want a record of every AI decision, tool call and operator action, so that I can investigate a complaint.
- US-S2. As a compliance officer, I want proof that card numbers never reach the model provider or the database, so that we follow card-data rules.
- US-S3. As a compliance officer, I want to delete a customer's data on request, so that we follow GDPR.

## 6. Functional requirements

Priority: **M** must, **S** should, **C** could, **W** won't (this version).

| ID | Requirement | Pri | Stories |
|---|---|---|---|
| FR-1 | Accept a customer message in a chat conversation (a ticket) and keep the full conversation. Follow-up messages continue the same ticket. | M | C1 |
| FR-2 | Show an AI disclosure at the start of every conversation; label every message as AI or human. | M | C2 |
| FR-3 | Mask card numbers (PAN), CVV and other payment secrets before the text is stored, logged or sent to a model. | M | C7, S2 |
| FR-4 | Classify the ticket: intent (12 classes), second intent if present, urgency, language, "wants a human". | M | O1, O4 |
| FR-5 | Search the knowledge base (hybrid search) and return the most relevant article sections. Customer answers use only articles marked `external`. | M | C4, O2 |
| FR-6 | Read the customer's own account data (profile, transactions, card) through read-only tools. The customer identity comes from the session, never from the model. | M | C3 |
| FR-7 | Write an answer that uses only the found sources and account facts, with a citation for every factual claim. | M | C4 |
| FR-8 | Check the draft: citations are valid, every claim is supported, no masked or internal data, policy rules are respected. One retry with a better search is allowed. | M | O3 |
| FR-9 | Decide the route: **auto-reply**, **operator approval** (draft waits), or **handoff** (human takes over). The decision uses fixed policy rules first, then a confidence score. | M | O1, O4, L2 |
| FR-10 | Always hand off: restricted accounts, lost/stolen card, suspected fraud, complaints and legal threats, requests for a money action (refund, limit increase, unfreeze), requests for a human, no answer in the knowledge base, detected prompt injection, non-English text. Dispute and login-security tickets may get an AI draft, but an operator must always approve it. | M | C5 |
| FR-11 | On handoff, tell the customer what happens next and the expected response time; give a safe first step when one exists (for example, freeze the card). | M | C5 |
| FR-12 | Operator console: queue, ticket view (conversation, summary, draft, sources, account facts, route reasons), approve / edit / reject, free reply, close. | M | O1–O5 |
| FR-13 | Resume the paused AI run after the operator decision; the run must survive a restart of the service. | M | O3 |
| FR-14 | Create a Jira issue (dispute, fraud, bug, limit request) and post to Slack for urgent cases through an MCP server. Jira creation needs operator approval. | M | O6 |
| FR-15 | Append-only audit log of AI decisions, tool calls, operator actions, logins and settings changes. | M | S1 |
| FR-16 | Record tokens, cost, latency, model and prompt version for every model call; trace every run. | M | L1 |
| FR-17 | Dashboard for the lead: volume, automation share, routes and reasons, operator edit rate, cost, latency, customer ratings. | M | L1, L3 |
| FR-18 | Settings for the lead: auto-reply on/off (kill switch), confidence threshold, daily model budget. | M | L2 |
| FR-19 | Customer can rate an answer (helpful / not helpful) and ask for a human at any time. | M | C5, C6 |
| FR-20 | Remember the customer between tickets: short summaries of earlier tickets are available to the AI and the operator. | S | O5 |
| FR-21 | Operator search in the knowledge base, including `internal` articles. | S | O7 |
| FR-22 | Delete or anonymise all data of one customer on request. | S | S3 |
| FR-23 | Offline evaluation command that produces a report with all metrics from the [eval plan](eval-plan.md); a small evaluation runs in CI. | M | L1 |
| FR-24 | Reload the knowledge base from files with one command; changed articles are re-indexed, unchanged ones are skipped. | M | — |
| FR-25 | Answer in the customer's language (multilingual). | C | — |
| FR-26 | Operator edits feed a review list for prompt and knowledge-base improvement. | C | L3 |
| FR-27 | AI executes money actions (refund, limit change, unfreeze). | W | — |
| FR-28 | Email and voice channels; document upload by customers. | W | — |

## 7. Non-functional requirements

Every number has a reason. "Target" is the goal for the MVP. "Red line" means: below this, auto-reply stays off (the cloud demo is the one exception, after P12-00: [ADR-0023](adr/0023-auto-reply-on-after-missed-red-lines.md)).

**Budget mode ([ADR-0018](adr/0018-budget-mode.md)).** NFR-4, NFR-5 and NFR-7 are measured on a small evaluation set (20 tickets, 16 attacks) and counted in tickets, not percentages. **These numbers are a demo check, not a proof.** Passing them shows that the demo works and that nothing broke badly. It does not show that the real error rate meets the target: 0 errors in 20 tickets still allows up to 16% (95% interval). The percentages in the "Reason" column say what a real rollout would have to prove (section 12).

| ID | Requirement | Target | Red line | Reason |
|---|---|---|---|---|
| NFR-1 | Time to answer, auto-reply, end to end | p50 ≤ 8 s, p95 ≤ 20 s | p95 ≤ 30 s | The pipeline has 4–6 model calls, and the answer is checked before the customer sees it, so it cannot be streamed word by word. Progress events are shown from second 1. Today's median is 3 h 40 min. |
| NFR-2 | Time to first visible reaction (progress event) | ≤ 1 s | ≤ 2 s | The customer must see at once that the message arrived. |
| NFR-3 | Model cost per ticket (average over all tickets) | ≤ €0.05 | ≤ €0.10 | A human ticket costs €4.50 (assumption). Even at 40% automation the AI must cost under about 3% of the saving. |
| NFR-4 | Wrong answer sent automatically (demo check on the 20 evaluation tickets) | 0 | 0 (any wrong auto-reply keeps auto-reply off) | A real rollout would need ≤ 3% (red line 5%) shown on at least 300 reviewed answers. Payments domain: a wrong answer is worse than a slow one. 3% is the level a trained human team reaches on routine answers (assumption). |
| NFR-5 | Must-escalate tickets that are escalated (recall; demo check on the 20 evaluation tickets) | all | 1 miss; hard-handoff tickets: none missed | Missing a fraud or restriction case is the worst error of the system. |
| NFR-6 | Card numbers that reach a model, a log or the database | 0 | 0 | Card-data rule (PCI DSS idea): do not store or send the PAN. |
| NFR-7 | Successful prompt-injection attacks in the attack suite (16 attacks, 2 per category) | 0 | 1 of 16 | Public chat input is untrusted; the AI has tools. |
| NFR-8 | Internal content shown to a customer | 0 | 0 | Internal procedures and compliance reasons must not leak. |
| NFR-9 | Capacity | 2,000 tickets/day, 10 runs at the same time | — | Peak day from the world file; about 300 tickets in the peak hour. |
| NFR-10 | Availability of the chat API | 99.5% (design goal; the free-tier demo is best effort) | — | If the model provider fails, tickets go to operators — support never stops. There is one provider (ADR-0018): the fallback model does not help when Anthropic itself is down, so then every ticket goes to an operator. |
| NFR-11 | A paused run survives a restart | 100% of paused runs can be resumed | 100% | Operators may answer hours later. |
| NFR-12 | Audit: every operator action and AI route decision has a record | 100% | 100% | Complaint investigation. |
| NFR-13 | Data retention | tickets 24 months; traces 30 days; audit log 5 years (all configurable) | — | GDPR storage limitation; payment record-keeping practice. |
| NFR-14 | Daily model budget cap with safe fallback | configurable, default $0.50/day in the demo | — | Public demo with private API keys; when the cap is reached, all tickets go to handoff. |
| NFR-15 | Test coverage of deterministic safety code (masking, policy rules, output checks, auth) | ≥ 95% lines | 90% | This code is the last line of defence and is cheap to test. |

## 8. Regulatory constraints

- **Card data.** No full card number, CVV, PIN or 2FA code in the database, logs, traces or model requests. Masking happens before anything else.
- **GDPR.** Data minimisation (IBANs are stored only partly masked, ADR-0017; emails, phone numbers and IBANs are replaced with placeholders before model calls), retention limits, deletion on request, no customer data in the evaluation sets (synthetic data only).
- **EU AI Act, Article 50** (the transparency duty for chatbots applies since 2 August 2026). The customer is told at the start that an AI system answers. AI-written messages are labelled. A customer-support chatbot is not a high-risk system, but the transparency duty applies.
- **Tipping-off.** The AI never explains why an account is restricted.
- **Anti-money-laundering rules.** Verification, restrictions and limit increases are human decisions. In a real payments company, legal record keeping (5 years) limits what can be deleted on a GDPR request; the erasure function of the Hub covers support data only.
- **Complaints.** A formal complaint needs a written answer from a human within 15 business days, so complaints are always handed off.

## 9. Success metrics

**Business (shown on the dashboard, estimated from the evaluation and the demo traffic)**
- Automation share: tickets that got an auto-reply — target ≥ 35% at NFR-4.
- AI-resolved share: auto-replied tickets that were closed with no human message, no request for a human and no "not helpful" rating. This is the honest number: an answer that makes the customer write again did not solve anything.
- Guard metrics: customer rating of AI answers not under the human level (78% positive today); share of customers who ask for a human after an AI answer.
- Median time to first answer: under 20 s for auto-replied tickets (today: 3 h 40 min).
- Cost per ticket: AI cost (measured) against €4.50 for a human (assumption).
- Operator edit rate: share of drafts approved without changes — target ≥ 60%.

**Quality (measured offline on a small set, see [eval plan](eval-plan.md); a demo check, not a proof)** — classification accuracy, retrieval hit rate, key-fact coverage, faithfulness (both judged by the same model family, unvalidated), escalation recall and precision, attack success rate.

**Business case (all inputs are assumptions from [the world file](tallybridge-world.md))**

| Item | Calculation | Per year |
|---|---|---|
| Tickets | 1,200 × 365 | 438,000 |
| Cost of human support today | 438,000 × €4.50 | about €1.97 million |
| Operator time freed at 35% automation | 153,300 tickets × €4.50 | about €690,000 |
| Model cost at the NFR-3 target | 438,000 × €0.05 | about €22,000 |

How to read it: the freed time is capacity (shorter queue, faster answers on hard cases, longer support hours), not a plan to reduce staff. The table does not include the cost of the team that builds and runs the Hub, and it does not count the time that prepared drafts save on approval tickets. The numbers come from a synthetic company; they show the size of the effect, not a forecast.

## 10. MVP boundary

The MVP is everything marked **M** above: one channel (chat), English, 12 intents, about 50 knowledge-base articles, read-only account tools, Jira and Slack actions, operator console, dashboard, evaluation, tracing, deployed demo. **S** items are built after all **M** items pass their acceptance checks. **C** and **W** items are not built.

## 11. Risks

| Risk | Answer in the design |
|---|---|
| Cheap but bad answers (the Klarna lesson) | Quality gates before automation; red lines; kill switch; easy handoff. |
| The model invents facts | Answers only from sources; citation check; groundedness check; no source → handoff. |
| Customers cannot reach a human | "Talk to a human" always visible; request → immediate handoff. |
| Prompt injection through the chat | Input is untrusted data; read-only tools bound to the session customer; fixed policy rules outside the model. |
| Synthetic data hides real problems | Stated openly in the README; at least 6 of the 20 tickets are hard cases; labels are checked by the reviewer subagent (no human gate since ADR-0018). |
| Public demo burns the API budget | Access code, rate limit, daily budget cap. |
| The evaluation set is far too small to prove a 3% error rate (20 tickets, a handful auto-replied, no test set; ADR-0018) | Results are reported as counts with confidence intervals and called a demo check, not a proof. The same model family judges the answers. Offline numbers only allow the next rollout step (section 12); they do not replace it. |
| The project budget runs out (about $5 of model calls in total) | One cheap model for every role, local embeddings, cached evaluation runs, paid runs only when the owner asks, a daily budget cap and a spending limit in the provider console (ADR-0018). |

## 12. Rollout plan

How a real company would put this system into operation. The demo supports every step with existing switches; steps 3 and 4 need real traffic, so in this project they are described, not done.

| Step | Setting | Exit condition |
|---|---|---|
| 1. Shadow | Auto-reply off. Every ticket goes to an operator; drafts are created but the operator sees them only after sending the own answer (in the demo: all drafts go to approval). | Offline red lines pass in the milestone run (in this project a demo check on 20 tickets; a real company would first build a larger held-out test set). |
| 2. Copilot | Auto-reply off. Operators approve, edit or reject drafts. | Share of drafts approved unchanged ≥ 60% for two weeks; no red-line breach in the evaluation runs (a real company would run them weekly; this project has no scheduled evaluation, ADR-0018). |
| 3. Limited automation | Auto-reply on for two low-risk intents (`fees_fx`, `limits`) through the allow-list, high threshold. A human reviews a random sample of auto-replies every week. | Wrong answers in the sample ≤ 3% with at least 300 reviewed answers. |
| 4. Wider automation | More intents are added one by one; the threshold is lowered along the risk–coverage curve. | Same check for every new intent. |

Any red-line breach or a wrong answer about money → kill switch, back to step 2, error analysis, new evaluation tickets.
