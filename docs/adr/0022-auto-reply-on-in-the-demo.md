# ADR-0022: Auto-reply on in the demo before the milestone run

- Status: accepted; decision 3 (the condition) superseded by [ADR-0023](0023-auto-reply-on-after-missed-red-lines.md)
- Date: 2026-11-24
- Supersedes: the "auto-reply stays off in the demo" part of [ADR-0019](0019-scope-cuts.md) (decision 1, P9-03; first consequence), and the precondition in the [evaluation plan](../eval-plan.md) §1 and §2 and the [PRD](../prd.md) (NFR-4, rollout stages) that auto-reply stays off until the milestone run (P12-00) passes the red lines. Owner decision on PR #68.

## Context
ADR-0019 cut the confidence-threshold work (P9-03, E9, H7) and kept auto-reply off in the demo, so the automation share was 0 by design. The deployment lock `AUTO_REPLY_ALLOWED` (security §3 G5, P10-02) enforced it: while it is `false`, a lead cannot switch auto-reply on and `decide` sends every draft to an operator. The demo now runs on Cloud Run (P10-07, H8 done), and the owner wants it to show the full route set, including an answer sent without review, before the milestone run exists.

## Decision
1. **The demo deploy sets `AUTO_REPLY_ALLOWED=true` for `hub-api`** (`.github/workflows/deploy.yml`, operations §6). The default in code, images, `infra/compose.prod.yml` and `.env.example` stays `false`.
2. **Nothing else changes.** The seeded `auto_reply_enabled` stays `false` and the threshold 0.95; a lead switches auto-reply on in the console (with reason and audit record) and can switch it off at any time. The routing rules of AI pipeline §6 still hand every risky ticket to a human (policy topics, restricted accounts, failed checks, injection, low confidence), and only intents of the policy's `auto_reply_allowlist` can be auto-replied.
3. **Condition:** if the milestone run (P12-00) misses any red line, the owner sets `AUTO_REPLY_ALLOWED=false` in `deploy.yml` and redeploys; it stays off until the breach is fixed and a new run passes.

Reasons: the Gate 8 attack run [`2026-11-20-attacks-rag_hybrid-f3e5696`](../../evals/runs/2026-11-20-attacks-rag_hybrid-f3e5696/report.md) had 0 of 16 successful attacks and 0 internal leaks; routing still hands risky topics to a human; the daily budget guard is $0.20 (`DAILY_BUDGET_USD=0.20`), which caps what a misuse of the public demo can cost.

## Alternatives considered
- **Wait for the milestone run (P12-00).** The rule of the evaluation plan, but the demo could not show the auto-reply route until the end of the project.
- **Unlock only for the screen recording.** Fewer days with auto-reply possible, but the deployed demo and the recorded one would differ.

## Consequences
- The deployed demo can auto-reply once a lead turns it on; the automation share in the reports is no longer 0 by design. The README and the case study must say that auto-reply was unlocked before the milestone run, on this decision.
- Twenty evaluation tickets cannot show a wrong-answer rate for auto-replies (ADR-0019's reason to cut E9 still holds); the protection is the routing rules, the allow-list, the high threshold and the lead's kill switch, not a measured rate.
- The repo's own defaults are unchanged, so local stacks, CI and the VPS fallback still keep auto-reply locked.
