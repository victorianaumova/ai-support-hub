# Error analysis: run 2026-11-24-rag_hybrid-055c0c6 (P12-00 milestone)

Variant `rag_hybrid`; Haiku 5.5 (Sonnet 5.5 fallback); triage `v1`, research `v2`, write `v5`, verify `v1`; judges key facts `v1`, faithfulness `v3`, citation `v3`. Clean tree at `055c0c6`, uncached (`NO_CACHE=1`).

## Red lines

| Metric | Value | Red line | Result |
|---|---|---|---|
| citation validity | 5 of 9 answered tickets with an invalid marker (CI 27–81%) | 1 | **fail** |
| key-fact coverage | 0.73 (9 tickets) | 0.75 | **fail** |
| triage accuracy | 16 of 20 (CI 58–92%) | 16 of 20 | pass, on the line |
| faithfulness | 2 of 8 with an unsupported claim (CI 7–59%); T-0013 not judged (malformed judge output) | 2 | pass, on the line |
| hard-handoff recall | 8 of 8 (CI 68–100%) | 1.00 | pass |
| escalation recall | 10 of 10 (CI 72–100%) | 1 miss | pass |

Per ADR-0022, a missed red line keeps auto-reply off.

**Compared with base `585ce6a`:** nothing changed beyond noise. Triage is down 1 (T-0018), invalid citations are up 1 (T-0002, a judge error) and unsupported claims are up 2.

## Citations: real errors or a strict judge?

The judge rejected 13 citations. Of these, 8 are judge errors:

- T-0001 [2], T-0002 [2] and [3]: the source says what the sentence says.
- T-0018 [4]: the source says "closed 30 days after your request" word for word.
- T-0013 [2]+[3], T-0018 [6]+[7]: two markers on one sentence. The judge rejected each marker, although its v3 rule says to check only that marker's part of the sentence.

The other 5 are real errors, in 4 tickets:

- T-0001 [3]: one account fact cited for three transactions.
- T-0008 [1], T-0013 [1]: a transaction cited to an article instead of its account fact (against write v5's rule on account facts).
- T-0018 [3], [5]: details taken from another source, and the unsupported claim that the two charges are "inactivity fees".

With a perfect judge, 4 of 9 tickets would still fail, against a red line of 1. **The miss is real.**

## Key facts (11 missing)

- 6 missing because the right section was not retrieved: T-0002, T-0006, T-0008 fact 2, T-0009, T-0013 fact 6, T-0018 fact 4.
- 3 missing because the writer left out facts that were in its sources: T-0008 fact 3, T-0018 facts 1 and 2.
- 1 is probably a wrong label: T-0013 fact 2.
- 1 was failed by the code check: T-0013 fact 5. The judge marked it "present", but its quote is not in the draft. Counting it would give exactly 0.75.

## Failure categories

| Category | Tickets |
|---|---|
| Right document not retrieved | 4 (T-0002, T-0006, T-0009, T-0018) |
| Right document retrieved, answer wrong | 4 (T-0001, T-0007, T-0008, T-0013) |
| Wrong intent | 1 (T-0005) |
| Wrong label in the data | 1 (T-0015) |
| Wrong route | 0 |
| Answer right, judge wrong | 0 tickets (but 8 rejected citations and 1 key fact) |

## Top patterns

1. **An account detail is cited with the wrong marker** (T-0001, T-0008, T-0013).
2. **The search finds the right article but not the section that holds the fact** (6 tickets).
3. **Triage gets only the newest message, not the history.** `triage_messages` in `apps/api/src/hub/graph/nodes/triage.py` fills only `{{ticket}}`, so T-0005 has been triaged wrong in every RAG run.

## Other findings

- **Automation share is 0 of 20.** 8 tickets go to approval with `policy_review`: normal urgency, no proposed actions. The only review rule that can match them is "customer has another open ticket with the same intent". Possible cause: open tickets that earlier runs left in the evaluation database. Not checked.
- **Faithfulness.** Verify also caught the unsupported claim in T-0018. T-0007's claim ("not the balance") breaks write v5's rule against drawing its own conclusions.

## Possible label errors

- T-0013 fact 2 still includes a conclusion from adding up amounts ("so … went over the limit"). PR #70 removed the same kind of conclusion from T-0008.
- T-0015: the intent and secondary intent are borderline. The route is the same either way.

## Suggested next fix (owner decides)

Write `v6` with one rule: cite every account detail with the marker of its own account fact, one marker per fact. Expected effect: real citation failures drop from 4 to 1 (T-0018). Measure it with one `make eval` and `BASE=2026-11-24-rag_hybrid-055c0c6`.

## Cost

Total paid: $0.034681, of which $0.019555 for answers (62 calls) and $0.015126 for judges (63 calls). The `cost_usd` field in `metrics.json` and the $0.0010 per ticket count only the answers, not the judges (`summary()` in `evals/src/hub_evals/report.py`).
