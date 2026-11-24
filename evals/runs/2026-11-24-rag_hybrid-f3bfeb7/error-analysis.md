# Error analysis: run 2026-11-24-rag_hybrid-f3bfeb7 (P12, write v6 and eval reset)

Variant `rag_hybrid`; Haiku 5.5 (Sonnet 5.5 fallback); triage `v1`, research `v2`, **write `v6`**, verify `v1`; judges key facts `v1`, faithfulness `v3`, citation `v3`. Clean tree at `f3bfeb7`, uncached. Base: `2026-11-24-rag_hybrid-055c0c6` (P12-00).

Only two things changed against the base. (1) `write/v6` adds one rule to v5: one marker per account fact. (2) The runner closes the open tickets left by earlier starts (`close_open_tickets`), so `memory_shows_open_case` no longer fires from leftover state.

`make eval-report` is not implemented yet (listed under `NOT_IMPLEMENTED` in the Makefile), so the comparison below was made by hand from both `results.jsonl` files.

## Red lines

| Metric | Value | Red line | Result |
|---|---|---|---|
| citation validity | 6 of 9 answered tickets with an invalid marker (CI 35–88%) | 1 | **fail** |
| key-fact coverage | 0.69 (9 tickets) | 0.75 | **fail** |
| triage accuracy | 16 of 20 (CI 58–92%) | 16 of 20 | pass, on the line |
| faithfulness | 2 of 9 with an unsupported claim (CI 6–55%) | 2 | pass, on the line |
| hard-handoff recall | 8 of 8 (CI 68–100%) | 1.00 | pass |
| escalation recall | 10 of 10 (CI 72–100%) | 1 miss | pass |
| wrong auto-reply | 0 of 0 (nothing was auto-replied) | any | pass, but nothing was tested (see "Automation") |

Per ADR-0022, a missed red line keeps auto-reply off.

**Compared with base `055c0c6`:** nothing changed beyond noise, apart from the route reasons.
- Triage: 16 of 20 in both runs, with the same 4 misses (T-0005, T-0013, T-0015, T-0018).
- Citations: 6 of 9 against 5 of 9. That is one ticket, so it is noise. Counting only real errors, it went from 4 tickets to 3, which is also noise.
- Key facts: 0.69 against 0.73. Two tickets changed, and the change is noise (see "Key facts").
- Faithfulness: 2 of 9 against 2 of 8.
- Routes: `policy_review` fell from 8 to 2. That change is real, and the runner reset caused it.

## Citations: did v6 fix what it targeted?

| Targeted ticket | Base error | This run |
|---|---|---|
| T-0001 | [3]: one account fact cited for three transactions | **Fixed.** Each transaction now has its own marker (A1, A2, A3), and the judge accepted all three. |
| T-0013 | [1]: the transaction was cited to `error-codes#ERR-1020` | **Fixed** for the transaction: TX-000046 now cites A6. But the tier is now cited to the limit fact: "Your Starter tier has a monthly receive limit of €1,000 [3]" cites A3, and only A1 states the tier. |
| T-0008 | [1]: the transaction was cited to `error-codes#ERR-3011` | **Not fixed.** The sentence is identical: "Your card payment TX-000126 of 1450.00 EUR was declined with error code ERR-3011 [1]" still cites the error-code chunk. A9 states this transaction and was in the writer's account facts. |

The judge rejected 12 of the 49 citations it judged. 8 of them are judge errors:

- **T-0001 [5]** (first use): "The money is available after 3 business days." The source says "Normally, the money is available after 3 business days." The base analysis found the same judge error on the same source.
- **T-0002 [3][4][6][7]**: "Your other GBP payments, TX-000131, TX-000130, TX-000129 and TX-000128, show as completed [3][4][5][6][7]." Each marker's account fact states that its own transaction is completed. The judge rejected four of the markers and accepted [5], whose case is exactly the same. Its v3 rule says to check only the part of the sentence that a marker is cited for, and here it did not. [7] repeats A5, which is a writer defect against v6 ("one marker per fact"), but the fact does support the sentence.
- **T-0002 [8]**: "A GBP payment by Faster Payments normally arrives on the same day." The source says "arrives on the same day". The added "normally" only weakens the claim.
- **T-0018 [4][5]**: "Two fees of 5.00 EUR were charged, on 2026-09-18 and 2026-10-18 [4][5]." A2 is the fee of 09-18 and A1 the fee of 10-18, in that order. Both are correct. This is the same stacked-marker rejection as in the base run (T-0013, T-0018).

The other 4 are real errors, in 3 tickets:

- **T-0008 [1]**: the transaction is cited to an article instead of its account fact. This is the error v6 targeted, and it is still there.
- **T-0013 [3]**: the tier detail is cited to the limit fact A3 instead of A1. This breaks v6, but it is a minor error.
- **T-0013 [6]**: "so you can contact us to find out why it was rejected [6]" cites `complaints#1`, which is about how to make a formal complaint. Verify flagged the same claim as unsupported.
- **T-0020 [2]**: "Your balance does not cover this error, so there is no need to add money for it [2]." This is the writer's own conclusion, which v5 forbids. Verify flagged it too.

With a perfect judge, 3 of 9 tickets would still fail (CI 12–65%), against a red line of 1. **The miss is real.** Even if T-0013 [3] counted as a judge error, T-0013 would still fail on [6].

The real errors moved but did not shrink much. T-0001 and T-0018 are clean now. T-0020 is newly wrong. T-0008 and T-0013 still fail. The base analysis expected v6 to bring the count to 1. It brought it to 3: 4 tickets became 3, a one-ticket change, which is noise.

## Key facts: 0.73 → 0.69 is noise

Only two tickets changed against the base. Every other ticket has the same coverage and the same missing facts.

- **T-0001: 1.00 → 0.75.** Key fact 4 says the three pending transactions are TX-000025, TX-000026 and TX-000027. The draft names all three with their amounts and says they are pending. The judge marked the fact present, but the code check did not find its quote in the draft (`quote_found: false`), so the fact counts as missing. This is a judge or quote-check error, not a writer error. Counting the fact would give 0.72.
- **T-0013: 0.50 → 0.33.** Key fact 4 says a Starter account cannot ask for a limit increase. In the base run the research loop retrieved `limit-increase#0`/`#3`, and the writer stated the fact. In this run it did not retrieve them. This is variance in the research loop, not an effect of v6.

So the drop is one quote-check artifact plus one retrieval difference: two tickets, which is noise. Coverage stays below the red line in both runs.

All 13 missing facts, in the base's grouping:
- **9 not retrieved:** T-0002 fact 3; T-0006 fact 4; T-0008 fact 2; T-0009 fact 3; T-0013 facts 4, 5 and 6; T-0018 facts 2 and 4.
  - T-0008 fact 2 ("pending card payments count toward the daily card spend limit") is now in `account-limits#1`, added by PR #70. The search found that chunk for T-0001 but not for T-0008.
  - T-0009 fact 3 answers the customer's direct question "how long till its checked again" (1–2 business days). The draft does not answer it.
- **2 the writer left out, although they were in its sources:** T-0008 fact 3 (account facts A7 and A8 list both pending payments); T-0018 fact 1 (`close-account#0` says the balance must be zero, and gives ERR-5012).
- **1 is probably a wrong label:** T-0013 fact 2 (see below).
- **1 was failed by the quote check:** T-0001 fact 4 (see above).

## Failure categories

| Category | Count | Tickets |
|---|---|---|
| Right document not retrieved | 4 | T-0002, T-0006, T-0009, T-0018 |
| Right document retrieved, answer wrong | 3 | T-0008, T-0013, T-0020 |
| Answer right, judge wrong | 1 | T-0001 (citation [5] and key fact 4), plus 7 judge errors in T-0002 and T-0018, which are classified by their key-fact misses |
| Wrong intent | 1 | T-0005 (handed off on `action_request`, so the miss has no effect on the route) |
| Wrong route | 1 | T-0007: the writer proposed `jira_create_issue` with `issue_type: dispute` for a merchant-category decline (ERR-3005, crypto). The label has `should_escalate: false`. The extra escalation is in the safe direction. |
| Wrong label in the data | 1 | T-0015 |

## Top patterns

1. **v6 did not change the writer on T-0008.** The base and this run produce a word-for-word identical first sentence, cited to the error-code chunk. The writer had the transaction as account fact A9, so the problem is not missing evidence. This is the third run in a row with this error on T-0008 (585ce6a, 055c0c6, f3bfeb7). Two prompt rules (v5 and v6) have not changed it.
2. **The citation judge rejects stacked markers.** 6 of the 8 judge errors are markers written side by side at the end of a sentence ("[3][4][5][6][7]", "[4][5]"). The judge accepts the same facts when each marker stands right after its detail, as in T-0001 [1][2][3]. v6's "each one gets the marker of its own fact" made the writer stack markers more often, so the judge's weakness now costs more. T-0002 went from 2 to 5 rejected markers.
3. **The search finds the article but not the section that holds the fact.** 9 of 13 missing key facts are of this kind, in 6 tickets. The research loop also varies between runs: T-0013 lost `limit-increase`, and T-0008 never gets `account-limits#1`. A writer prompt cannot fix this.

## Automation share and route reasons

Automation share is 0 of 20 (CI 0–16%). The routes are approval 9 and handoff 11. The route reasons below are confirmed from `results.jsonl`.

- **`policy_review`: 2 tickets.**
  - T-0007: the draft has one proposed action (`jira_create_issue`, `issue_type: dispute`), which matches the `approval_only` conditions `has_proposed_actions` and actions `[dispute]`.
  - T-0008: triage urgency `high`, which matches the condition `high_urgency`. The draft has no proposed action. Its urgency was `high` in the base run as well, which plausibly fits "travel manager stuck at the desk".
  - In the base run, T-0007 had no proposed action, so its `policy_review` there came from the open-case memory. The proposed action is new writer variance.
- **`auto_reply_off` as the only reason: 5 tickets** (T-0001, T-0002, T-0006, T-0009, T-0018).
  - `decide` reaches this reason only after verify, the degraded check and the review rules have all passed (`apps/api/src/hub/graph/routing.py`).
  - The evaluation never allows auto-reply: the runner builds `Pipeline(...)` without `auto_reply_allowed`, which defaults to `False` (`evals/src/hub_evals/runner.py`, `apps/api/src/hub/graph/pipeline.py`). All five predicted intents are in `auto_reply_allowlist`.
  - The confidence score is not in `results.jsonl`, so whether each ticket would also pass the 0.95 threshold is not known.
- **`unsupported_claims`: 2 tickets** (T-0013, T-0020). Verify catches them, before the review rules. Both were `policy_review` in the base run.
- The remaining 11 tickets are handoffs, with the same reasons as in the base run, apart from small changes in the reason lists of T-0005 and T-0014.
- The reset worked: 6 of the base's 8 `policy_review` tickets lost that reason.

**Would any of the 5 have been a wrong auto-reply?** Yes, all 5 under the metric's rule (key-fact coverage < 0.8). All five are faithful, and none should escalate.

| Ticket | Coverage | Assessment |
|---|---|---|
| T-0001 | 0.75 | Only because of the quote-check error. Corrected to 1.00, this answer is right. |
| T-0002 | 0.67 | Does not tell the customer to check messages and upload an invoice or contract. A minor gap. |
| T-0006 | 0.75 | Does not say that the failed withdrawal is free. A minor gap. |
| T-0009 | 0.67 | Does not answer the customer's direct question "how long". A real gap. |
| T-0018 | 0.40 | Does not explain the two €5 fees (it hedges with "our help-centre describes an inactivity fee") and does not say the balance must be zero. A real gap. |

So switching auto-reply on with this pipeline would breach the wrong-auto-reply red line on these tickets: 5 of 5 by the metric (CI 57–100%), 4 of 5 after correcting the T-0001 judge error (CI 38–96%). This assumes they would pass the confidence threshold, which this run does not record. The only reason auto-reply did not breach it is that it was never switched on.

## Possible label errors

- **T-0013 fact 2** still contains a conclusion drawn from adding up amounts ("so … went over the limit"). PR #70 removed this kind of conclusion from T-0008. This is unchanged from the base analysis.
- **T-0015**: the intent and the secondary intent are borderline. The route is the same either way. Unchanged from the base analysis.

## Suggested next fix (owner decides)

**Change:** fix the citation judge for stacked markers, as eval plan §5 prescribes for a wrong verdict. Write `judge_citation/v4`: when several markers stand together, each one is judged only against the item of the sentence that its source states. Add T-0002 [3]/[4]/[6] and T-0018 [4]/[5] as recorded unit tests.

**Expected effect:** the measured citation validity moves from 6 of 9 to the real 3 of 9. The red line stays missed. The number then reflects the writer and not the judge, and the next writer change can be measured honestly.

**How to measure:** `make eval-rejudge RUN=evals/runs/2026-11-24-rag_hybrid-f3bfeb7` (judge calls only, about $0.015, no answer calls), then compare with this run.

**The real errors are not fixed by this.** They have three different causes:
- T-0008: an account detail cited to an article, which two prompt rules have not changed.
- T-0013: a claim from a section that was not retrieved.
- T-0020: the writer's own conclusion.

No single writer rule is likely to bring them to 1 of 9. The step most likely to stop the T-0008 kind of error is deterministic, not another prompt rule: a verify code check that a sentence naming a `TX-` id carries the marker of the account fact that states that id. That changes verify, so it is a separate decision. Otherwise, record citation validity as a known limitation, as after P7-07.

## Cost and latency

- Total paid: $0.034514. Of this, $0.019607 was for answers (66 calls) and $0.014907 for judges (67 calls).
- The $0.0010 per ticket in the report counts answers only.
- Latency of all runs: p50 2.8 s, p95 11.7 s. There were no auto-replied runs, so the latency targets were not measured.

## Draft row for docs/experiments.md

| 2026-11-24 | [`2026-11-24-rag_hybrid-f3bfeb7`](../evals/runs/2026-11-24-rag_hybrid-f3bfeb7/report.md) (P12; [error analysis](../evals/runs/2026-11-24-rag_hybrid-f3bfeb7/error-analysis.md)) | Does one marker per account fact fix the citation errors, and does the eval reset free the tickets that had no reason to wait for review? | Base `055c0c6`; `write/v6` (one marker per account fact); runner closes leftover open tickets before each start; everything else unchanged; no cache | Triage 16 of 20 (CI 58–92%); hit@5 8 of 9; key-fact coverage 0.69 (9) *judged*, red line missed (2-ticket change against 0.73, noise; 0.72 without one quote-check error); unsupported 2 of 9 (CI 6–55%) *judged*; invalid citation 6 of 9 (CI 35–88%) *judged*, red line missed, 3 of 9 real by analysis (T-0008 unchanged; T-0001 and T-0013 transaction markers fixed); hard-handoff 8 of 8; escalation 10 of 10; automation 0 of 20, `policy_review` 8 → 2, 5 tickets end at `auto_reply_off` only and all 5 have coverage < 0.8 | $0.0345 paid ($0.0196 answers + $0.0149 judges); all runs p50 2.8 s / p95 11.7 s | Owner decides; auto-reply stays off (ADR-0022) |
