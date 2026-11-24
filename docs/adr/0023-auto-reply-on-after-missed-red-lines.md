# ADR-0023: Auto-reply on in the demo after missed red lines

- Status: accepted
- Date: 2026-11-24
- Supersedes: only the condition of [ADR-0022](0022-auto-reply-on-in-the-demo.md) (decision 3: "if the milestone run (P12-00) misses any red line, the owner sets `AUTO_REPLY_ALLOWED=false` … it stays off until the breach is fixed and a new run passes"), and for the cloud demo only the rule of the [evaluation plan](../eval-plan.md) §1 and §2 and the [PRD](../prd.md) §7 that a red line missed in the milestone run keeps auto-reply off. Decisions 1 and 2 of ADR-0022 stay. Owner decision 2026-11-24.

## Context
The milestone run and one targeted fix both missed two red lines on the 20 dev tickets:

| Red line | Milestone run [`2026-11-24-rag_hybrid-055c0c6`](../../evals/runs/2026-11-24-rag_hybrid-055c0c6/report.md) | Fix run [`2026-11-24-rag_hybrid-f3bfeb7`](../../evals/runs/2026-11-24-rag_hybrid-f3bfeb7/report.md) (`write/v6`) | Red line |
|---|---|---|---|
| Citation validity *(judged)*: answered tickets with an invalid marker | 5 of 9 (4 real by error analysis) | 6 of 9 (3 real by error analysis) | 1 |
| Key-fact coverage *(judged)* | 0.73 | 0.69 | 0.75 |

All other red lines held in both runs: hard-handoff recall 8 of 8, escalation recall 10 of 10, triage 16 of 20 and faithfulness 2 unfaithful answers (both exactly on the line), and the attack run `2026-11-24-attacks-rag_hybrid-055c0c6` had 0 of 16 successful attacks and 0 internal leaks.

The error analyses ([milestone](../../evals/runs/2026-11-24-rag_hybrid-055c0c6/error-analysis.md), [fix](../../evals/runs/2026-11-24-rag_hybrid-f3bfeb7/error-analysis.md)) name three known error types:

- **Wrong source:** an account detail cited to a help-centre section or to another account fact (T-0008, T-0013).
- **Missed fact:** the right section is not retrieved, or the writer leaves out a fact its sources hold (T-0013, and six tickets in the milestone run).
- **Own conclusion:** a sentence states something no cited source says (T-0020).

Under ADR-0022 decision 3 auto-reply would go off. The Hub is a portfolio demo of a fictional company (Tallybridge, synthetic data, no real customers or money), and the owner wants the deployed demo to show the full route set.

## Decision
1. **Auto-reply stays allowed in the cloud demo** (`AUTO_REPLY_ALLOWED=true` for `hub-api`, as ADR-0022 decision 1) although the two red lines are missed. A lead may keep `auto_reply_enabled` on.
2. **Nothing in code, configuration or thresholds changes.** The red lines and targets stay as they are and keep being reported as missed; the defaults in code, images, `infra/compose.prod.yml` and `.env.example` stay `false`.
3. **The demo states it.** The README and the case study say that auto-reply is on although citation validity and key-fact coverage missed their red lines, with links to both runs. The customer chat keeps its notice that the assistant can make mistakes and that a human can be asked for at any time (`AI_NOTICE`, `apps/web/src/chat/ChatPage.tsx`).

What still protects the demo:

- **Allow-listed intents only:** only intents of the policy's `auto_reply_allowlist` can be auto-replied; a lead can narrow the list, never widen it.
- **Verify step:** every draft goes through the code checks and the claim verifier; an unsupported claim, a failed check or a failed mask sends the ticket to approval or handoff.
- **Confidence floor:** the rule score must reach `confidence_threshold` (seeded 0.95), and retrieval below `top_score_floor` gives a confidence of 0.
- **Risky topics and restricted accounts hand off:** the routing rules of AI pipeline §6 (policy intents, fraud, complaints, requests for a human, action requests, injection, other languages, restricted accounts) hand off before any auto-reply.
- **Kill switch:** a lead can switch auto-reply off in the console at any time; switching off is always allowed and audited.
- **Daily budget:** `DAILY_BUDGET_USD` (G8) caps what the public demo can spend.

## Alternatives considered
- **Follow ADR-0022 decision 3 and switch auto-reply off.** The safe rule, and the one a real product follows; the demo would then not show the auto-reply route.
- **Tune further until the red lines pass.** More prompt versions tuned on the 20 dev tickets, which are also the tuning set; a pass would be optimistic and would not prove the error rate.
- **Lower the red lines.** Refused: red lines are not changed to make a run pass (AGENTS.md rule 6).

## Consequences
- The demo can send an answer with a wrong citation or a missing fact without review. The known error types above are what to expect; an answer about the customer's own account carries the most risk.
- **A real product would keep auto-reply off** until the red lines pass on a larger held-out set whose judges are validated against human labels (eval plan §5; PRD NFR-4: at least 300 reviewed answers). Twenty tickets and an unvalidated same-family judge cannot show the wrong-answer rate.
- The automation share of the evaluation stays 0 of 20: the evaluation stack keeps the deployment lock, so it measures the routing up to `auto_reply_off`, not auto-replies.
