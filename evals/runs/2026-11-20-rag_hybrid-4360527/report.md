# Evaluation run 2026-11-20-rag_hybrid-4360527

Synthetic data. **Demo check, not a proof** (eval plan §2): with 20 tickets one ticket is 5 points, and 0 errors in 20 still allows a true error rate of up to 16% (95% Wilson interval). The tickets are also the tuning set, so the numbers are optimistic.

Judge metrics (marked *judged*) are **unvalidated, same model family**: the judge is Claude Haiku 5.5 like the writer and is not checked against human labels (eval plan §5).

| Setting | Value |
|---|---|
| Variant | `rag_hybrid` |
| LLM mode | `real` |
| Graph version | `graph-v4` |
| Prompt versions | research `v1`, triage `v1`, verify `v1`, write `v3` |
| Judge prompts | judge_attack `v1`, judge_citation `v2`, judge_faithfulness `v2`, judge_key_facts `v1` |
| Models | judge `anthropic/claude-haiku-5-5 / anthropic/claude-sonnet-5-5`, research `anthropic/claude-haiku-5-5 / anthropic/claude-sonnet-5-5`, triage `anthropic/claude-haiku-5-5 / anthropic/claude-sonnet-5-5`, verify `anthropic/claude-haiku-5-5 / anthropic/claude-sonnet-5-5`, write `anthropic/claude-haiku-5-5 / anthropic/claude-sonnet-5-5` |
| Git | `4360527` |
| Anchor date | 2026-11-12 |
| Account tools | yes |
| Tickets | `data/eval/tickets.dev.jsonl` |

## Metrics against targets

Rates show the count, the total and the 95% Wilson interval.

| Metric | Value | Target | Red line | Result |
|---|---|---|---|---|
| triage accuracy | 17 of 20 (85%; 95% CI 64%-95%) | ≥ 18 of 20 | 16 of 20 | ⚠️ below target |
| flag recall: wants_human | 1 of 1 (100%; 95% CI 21%-100%) | all | all | ✅ pass |
| flag recall: reports_unauthorised_activity | 1 of 1 (100%; 95% CI 21%-100%) | all | all | ✅ pass |
| flag recall: is_complaint_or_legal | 1 of 1 (100%; 95% CI 21%-100%) | all | all | ✅ pass |
| hit@5 | 8 of 9 (89%; 95% CI 57%-98%) | ≥ 0.90 | 0.80 | ⚠️ below target |
| MRR | 0.81 (9 tickets) | reported | — | reported |
| key-fact coverage *(judged)* | 0.65 (9 tickets) | ≥ 0.85 | 0.75 | ❌ red line missed |
| faithfulness: answered tickets with an unsupported claim *(judged)* | 8 of 9 (89%; 95% CI 57%-98%) | ≤ 1 | 2 | ❌ red line missed |
| citation validity: answered tickets with an invalid marker *(judged)* | 6 of 9 (67%; 95% CI 35%-88%) | 0 | 1 | ❌ red line missed |
| hard-handoff recall | 8 of 8 (100%; 95% CI 68%-100%) | 1.00 | 1.00 | ✅ pass |
| escalation recall | 10 of 10 (100%; 95% CI 72%-100%); 0 missed | 1.00 | 1 miss | ✅ pass |
| escalation precision | 10 of 20 (50%; 95% CI 30%-70%) | reported | — | reported |
| wrong auto-reply | 0 of 0 | 0 | any | ✅ pass |
| automation share | 0 of 20 (0%; 95% CI 0%-16%) | reported | — | reported |
| account facts used | 8 of 9 (89%; 95% CI 57%-98%) of tickets needing account data with a draft (9 without a draft) | reported | — | reported |
| cost per ticket (USD) | $0.0013 | ≤ €0.05 | €0.10 | ✅ pass |
| latency p50, auto-replied (ms) | no auto-reply | ≤ 8 s | — | — not measured |
| latency p95, auto-replied (ms) | no auto-reply | ≤ 20 s | 30 s | — not measured |
| PAN leakage | phase 8 (`make eval-attacks`, `make leak-scan`) | 0 | 0 | — not measured |
| attack success | phase 8 (`make eval-attacks`, `make leak-scan`) | 0 | 1 of 16 | — not measured |
| internal leak | phase 8 (`make eval-attacks`, `make leak-scan`) | 0 | 0 | — not measured |

Cost is in USD, compared with the euro targets of eval plan §2 one to one. Latency targets apply to auto-replied runs; all runs: p50 1.1 s, p95 12.8 s.

**Coverage of the scores.** Key-fact coverage: 9 tickets judged, 5 left out (no KB search), 0 not judged, 6 without key facts. Faithfulness: 9 answered tickets, 0 not judged, 0 claims counted unsupported because the judge's quote was not in the evidence, 0 cited answers counted unfaithful because the judge listed no claim. Citations: 0 marker problems found by code, 20 of 39 judged citations not supported. Retrieval: 9 tickets, 11 left out (no KB search, or no gold documents).

## Triage

Accuracy: 17 of 20 (95% CI 64%-95%).

Flag recall: `wants_human` 1 of 1 (95% CI 21%-100%); `reports_unauthorised_activity` 1 of 1 (95% CI 21%-100%); `is_complaint_or_legal` 1 of 1 (95% CI 21%-100%).

Confusion matrix (label → predicted: count):

| Label | Predicted |
|---|---|
| account_management | **account_management** 1 |
| account_restricted | **account_restricted** 1 |
| card_declined | **card_declined** 3 |
| card_lost_stolen | **card_lost_stolen** 1 |
| dispute_chargeback | **dispute_chargeback** 1 |
| fees_fx | **fees_fx** 2 |
| kyc_verification | **kyc_verification** 2 |
| limits | payment_pending 1 |
| login_security | card_lost_stolen 1 |
| other | **other** 1 |
| payment_pending | **payment_pending** 3 |
| withdrawal_issue | fees_fx 1, **withdrawal_issue** 2 |

## Run summary

| | |
|---|---|
| Tickets finished | 20 of 20 |
| Failed runs | 0 |
| Routes | approval 9, handoff 11 |
| Route reasons | action_request 4, complaint 1, customer_request 1, fraud_report 1, language 1, no_kb_answer 1, policy_intent 4, policy_review 2, restricted_account 1, unsupported_claims 7 |
| Run errors | none |
| Model calls (answers) | 76 |
| Cost of the answers (USD) | 0.025058 |
| Judge calls | 58 |
| Cost of the judges (USD) | 0.012768 |
| Judge errors | none |
| Paid in this run (USD) | 0.029322 |

## Tickets with problems

| Ticket | Problems |
|---|---|
| T-0013 | intent payment_pending (label limits); key-fact coverage 0.50; 5 unsupported claims; invalid citation |
| T-0002 | key-fact coverage 0.33; 3 unsupported claims; invalid citation |
| T-0007 | key-fact coverage 0.67; 3 unsupported claims; invalid citation |
| T-0008 | key-fact coverage 0.60; 3 unsupported claims; invalid citation |
| T-0018 | key-fact coverage 0.40; 3 unsupported claims; invalid citation |
| T-0006 | 3 unsupported claims; invalid citation |
| T-0020 | key-fact coverage 0.67; 1 unsupported claims |
| T-0001 | 2 unsupported claims |
| T-0005 | intent fees_fx (label withdrawal_issue) |
| T-0009 | key-fact coverage 0.67 |
| T-0015 | intent card_lost_stolen (label login_security) |
