# Requirements trace

Each requirement of the [PRD](prd.md) §6 and §7, and the test or metric that shows it is met (build plan §3: "All **M** requirements of the PRD have a passing acceptance test or eval metric"). Paths are under `apps/api/tests/` unless they start with another folder. `e2e:` means a scenario in `apps/web/e2e/scenarios.spec.ts` (Playwright, fake models, in CI).

Numbers come from three runs, all **synthetic data, a demo check, not a proof**:

- the milestone fix run [`2026-11-24-rag_hybrid-f3bfeb7`](../evals/runs/2026-11-24-rag_hybrid-f3bfeb7/report.md) (20 dev tickets; judged metrics come from an unvalidated judge of the same model family as the writer);
- the attack run [`2026-11-24-attacks-rag_hybrid-055c0c6`](../evals/runs/2026-11-24-attacks-rag_hybrid-055c0c6/report.md) (16 attacks);
- the load smoke in the [README](../README.md#numbers) (fake models, local stack).

Status: **pass** = a test passes or the metric meets its red line; **missed** = the red line is missed; **partly** = the test covers the mechanism but not the whole requirement; **not built** = out of scope by an ADR or by priority.

## Functional requirements

| ID | Pri. | Requirement (short) | Proof | Status |
|---|---|---|---|---|
| FR-1 | M | Chat ticket keeps the conversation; follow-ups continue it | `integration/test_tickets_api.py::test_get_ticket_and_messages_after`, `::test_message_on_an_answered_ticket_follows_ownership`; `integration/test_human_review.py::test_a_follow_up_starts_a_run_only_while_the_ai_owns_the_ticket` | pass |
| FR-2 | M | AI disclosure; every message labelled AI or human | `unit/test_tickets_api.py::test_ticket_view_carries_the_ai_disclosure`; `integration/test_console_drafts.py::test_approve_sends_the_draft_with_the_reviewed_by_label`, `::test_edit_sends_the_operators_text_with_the_operator_label`; e2e: "an operator edits and sends" | pass |
| FR-3 | M | Mask PAN, CVV and secrets before storing, logging or a model call | `unit/test_masking.py` (incl. `::test_any_luhn_valid_number_in_random_text_is_masked`, Hypothesis); `unit/test_llm_gateway.py::test_a_request_with_card_data_is_blocked_and_audited`; `integration/test_tickets_api.py::test_card_number_is_stored_masked_and_never_in_the_database_or_logs`; e2e: "a customer pastes a card number and sees it masked". Known limits: 14 of 16 G1 probes keep their digits ([attack report](../evals/runs/2026-11-24-attacks-rag_hybrid-055c0c6/report.md), security §3) | pass, with known limits |
| FR-4 | M | Triage: intent, second intent, urgency, language, wants a human | `unit/test_triage.py::test_an_example_ticket_gets_the_expected_intent_and_flags`; metric: triage accuracy 16 of 20 (target 18, red line 16), flag recall 1 of 1 each ([report](../evals/runs/2026-11-24-rag_hybrid-f3bfeb7/report.md)) | pass, on the red line |
| FR-5 | M | Hybrid KB search; customers get `external` articles only | `integration/test_kb_index.py::test_an_error_code_query_returns_that_code_first_in_hybrid_mode`, `::test_external_search_never_returns_an_internal_chunk`; `unit/test_graph_nodes.py::test_research_searches_with_the_masked_ticket_text_and_keeps_external_chunks`; metric: hit@5 8 of 9 (target 0.90, red line 0.80) | pass, below target |
| FR-6 | M | Read own account data through read-only tools; identity from the session | `unit/test_tool_permissions.py::test_identity_is_injected_from_the_scope`, `::test_a_model_supplied_customer_ref_is_denied_even_when_it_is_the_right_one`; `integration/test_tool_calls.py`; metric: account facts used 8 of 9 | pass |
| FR-7 | M | Answer only from sources and account facts, a citation per claim | `unit/test_output_checks.py::test_an_answer_without_any_citation_fails`, `::test_a_citation_of_a_source_the_writer_was_not_given_fails`; metrics *(judged)*: unsupported claim 2 of 9 (red line 2), invalid citation 6 of 9 (red line 1), key-fact coverage 0.69 (red line 0.75) | **missed** (citation validity, key-fact coverage) |
| FR-8 | M | Verify citations, claims, masked and internal data, policy; one retry | `unit/test_verify_node.py`; `unit/test_output_checks.py::test_text_copied_from_an_internal_chunk_fails`; `unit/test_graph_nodes.py::test_verify_goes_back_to_research_once_when_the_check_found_unsupported_claims`, `::test_the_retry_loop_cannot_run_twice` | pass |
| FR-9 | M | Route: auto-reply, approval or handoff; rules first, then confidence | `unit/test_routing.py::test_every_row_of_the_decision_table_has_a_test` and the row tests; `unit/test_policy.py`; e2e: "a customer asks a routine question and gets a cited answer (auto-reply on)" | pass |
| FR-10 | M | Always hand off the listed cases; disputes and login security always approved | `unit/test_policy.py::test_each_handoff_intent_is_a_policy_intent`, `::test_each_handoff_flag_has_its_reason`, `::test_a_restricted_customer_is_handed_off_once_the_profile_is_known`, `::test_each_approval_only_rule_needs_review`; metric: hard-handoff recall 8 of 8, escalation recall 10 of 10; attacks 16 of 16 handed off | pass |
| FR-11 | M | Handoff notice with next step and response time; safe first step | `integration/test_graph_run.py::test_a_lost_card_ticket_gets_the_freeze_step_and_a_handoff_notice_in_the_stream`, `::test_a_question_outside_the_kb_is_handed_off_with_a_notice`; `unit/test_policy.py::test_safe_first_steps_come_from_every_matching_rule`; e2e: "fraud report: handoff notice, operator replies, customer sees the reply" | pass |
| FR-12 | M | Operator console: queue, ticket view, approve / edit / reject, reply, close | `integration/test_console_api.py::test_queue_filters_and_order`, `::test_ticket_detail`, `::test_close`; `integration/test_console_drafts.py`; e2e: "an operator approves a draft…", "an operator edits and sends", "a stale draft shows the conflict banner" | pass |
| FR-13 | M | Resume a paused run after the decision; survive a restart | `integration/test_human_review.py::test_a_paused_run_survives_a_restart_and_an_approve_completes_it`, `::test_recover_continues_a_run_from_its_last_checkpoint`; `integration/test_privacy_jobs.py::test_recover_runs_resumes_a_lost_approve_with_the_rebuilt_decision` | pass |
| FR-14 | M | Jira issue (operator-approved) and urgent Slack post through MCP | `integration/test_tool_calls.py::test_an_approved_jira_issue_is_created_once_per_ticket`; `integration/test_actions.py::test_every_urgent_handoff_notice_also_sends_the_slack_alert`; `unit/test_tool_permissions.py::test_an_operator_approval_does_not_cover_the_slack_alert`. Jira and Slack are mocks in the MCP server ([ADR-0019](adr/0019-scope-cuts.md)) | pass (mocks) |
| FR-15 | M | Append-only audit log | `integration/test_db_roles.py::test_update_on_audit_log_fails_for_the_app_role`, `::test_removing_audit_rows_fails_for_the_app_role`; `integration/test_audit_log.py::test_changing_one_stored_row_by_hand_is_reported_at_that_row`; on Neon checked in the deploy (PR #68) | pass |
| FR-16 | M | Tokens, cost, latency, model, prompt version per call; trace every run | `unit/test_llm_gateway.py::test_a_successful_call_records_tokens_cost_latency_model_and_prompt_version`; `integration/test_llm_calls.py`; `integration/test_graph_run.py::test_a_traced_run_stores_its_trace_id_and_traces_every_node`; `unit/test_tracing.py::test_trace_safe_masks_card_data_and_drops_restriction_reasons` | pass |
| FR-17 | M | Lead dashboard | `integration/test_metrics_api.py::test_the_summary_matches_the_seeded_rows`, `::test_the_summary_totals_agree_with_plain_sql`; e2e: "lead pages pass the accessibility check" | pass |
| FR-18 | M | Settings: kill switch, threshold, daily budget | `integration/test_metrics_api.py::test_a_lead_changes_settings_and_each_change_is_audited_with_its_reason`, `::test_switching_auto_reply_on_is_refused_while_the_deployment_does_not_allow_it`; e2e: "a lead switches auto-reply off and the next ticket goes to approval" | pass |
| FR-19 | M | Rate an answer; ask for a human at any time | `integration/test_tickets_api.py::test_feedback_on_an_ai_answer_is_stored_with_a_masked_comment`, `::test_request_human_hands_off_once_and_is_audited` | pass |
| FR-20 | S | Customer memory between tickets | `integration/test_memory.py::test_a_second_ticket_of_the_customer_has_the_first_ones_summary_in_context`, `::test_a_restricted_customers_summary_never_sees_internal_text` | pass |
| FR-21 | S | Operator KB search incl. `internal` | `integration/test_kb_api.py::test_console_search_includes_internal_articles`, `::test_the_console_shows_internal_articles_to_operators_only` | pass |
| FR-22 | S | Erase or anonymise one customer | `integration/test_privacy_jobs.py::test_erasure_leaves_no_message_memory_or_name_and_the_chain_verifies` | pass |
| FR-23 | M | Offline evaluation report with all metrics; a small evaluation in CI | `integration/test_eval_runner.py::test_judges_flags_and_metrics`; `evals/tests/test_report.py`; reports in `evals/runs/`. The real-model evaluation does not run in CI ([ADR-0018](adr/0018-budget-mode.md)); CI runs the runner with fake models only | partly (no real-model eval in CI, by ADR-0018) |
| FR-24 | M | Reload the KB with one command; only changed articles re-indexed | `integration/test_kb_loader.py::test_loading_twice_changes_nothing`, `::test_editing_one_article_updates_only_its_chunks`; `integration/test_kb_api.py::test_admin_reindex_loads_postgres_and_qdrant_and_is_audited` | pass |
| FR-25 | C | Answer in the customer's language | not built: non-English tickets hand off (`unit/test_policy.py::test_a_message_that_is_not_known_to_be_english_is_handed_off`) | not built |
| FR-26 | C | Operator edits feed a review list | not built (`make export-review-candidates`, removed with phase 9 by [ADR-0019](adr/0019-scope-cuts.md)) | not built |
| FR-27 | W | AI executes money actions | won't: no model has a tool with a side effect (AGENTS.md rule 4) | not built |
| FR-28 | W | Email, voice, uploads | won't | not built |

## Non-functional requirements

| ID | Requirement | Target / red line | Measured | Proof | Status |
|---|---|---|---|---|---|
| NFR-1 | Time to answer, auto-reply | p50 ≤ 8 s, p95 ≤ 20 s / p95 ≤ 30 s | not measured for auto-replies: the evaluation sends nothing automatically. All runs: p50 2.8 s, p95 11.7 s | [report](../evals/runs/2026-11-24-rag_hybrid-f3bfeb7/report.md) | not measured |
| NFR-2 | First visible reaction | ≤ 1 s / ≤ 2 s | not measured as a number. The stream sends `ticket.created` and a status event per step, and pings while the run works | `unit/test_graph_nodes.py::test_the_stream_shows_each_step_once_and_b0_never_searches_or_checks`; `integration/test_graph_run.py::test_the_stream_pings_while_the_run_works` | not measured |
| NFR-3 | Model cost per ticket | ≤ €0.05 / ≤ €0.10 | $0.0010 (answers; USD compared 1:1) | [report](../evals/runs/2026-11-24-rag_hybrid-f3bfeb7/report.md) | pass |
| NFR-4 | Wrong auto-reply (20 tickets) | 0 / 0 | 0 of 0: nothing was auto-replied in the evaluation, so nothing was tested | [report](../evals/runs/2026-11-24-rag_hybrid-f3bfeb7/report.md); [ADR-0023](adr/0023-auto-reply-on-after-missed-red-lines.md) | pass, but empty |
| NFR-5 | Escalation recall | all / 1 miss; hard handoff none missed | 10 of 10; hard handoff 8 of 8 | [report](../evals/runs/2026-11-24-rag_hybrid-f3bfeb7/report.md) | pass |
| NFR-6 | Card numbers in a model call, log or database | 0 / 0 | 0 real. The leak scan below found 2 hits, both false positives | tests of FR-3; [leak scan](#leak-scan-2026-11-24) | pass, with a scanner false positive to fix |
| NFR-7 | Successful attacks (16) | 0 / 1 of 16 | 0 of 16 | [attack report](../evals/runs/2026-11-24-attacks-rag_hybrid-055c0c6/report.md) | pass |
| NFR-8 | Internal content shown to a customer | 0 / 0 | 0 internal canaries in answers | [attack report](../evals/runs/2026-11-24-attacks-rag_hybrid-055c0c6/report.md); `unit/test_output_checks.py::test_a_canary_fails` | pass |
| NFR-9 | Capacity: 2,000 tickets a day, 10 parallel runs | — | 10 parallel customers, 50 tickets, 0 errors, 0 lost runs, with fake models on a laptop; real-model capacity not measured | [README load smoke](../README.md#numbers) | partly |
| NFR-10 | Availability 99.5% (design goal) | — | not measured (free-tier demo, best effort). A failed model call hands the ticket to a human | `integration/test_graph_run.py::test_a_failed_model_call_hands_the_ticket_to_a_human` | not measured |
| NFR-11 | A paused run survives a restart | 100% / 100% | test passes | `integration/test_human_review.py::test_a_paused_run_survives_a_restart_and_an_approve_completes_it` | pass |
| NFR-12 | Audit record for every operator action and route decision | 100% / 100% | tests pass | `integration/test_graph_run.py::test_the_run_writes_its_audit_records_without_ticket_text`; `integration/test_tickets_api.py::test_audit_records_ticket_message_and_masking_with_a_valid_chain`; `integration/test_metrics_api.py::test_a_lead_changes_settings_and_each_change_is_audited_with_its_reason` | pass |
| NFR-13 | Retention | tickets 24 months, traces 30 days, audit 5 years | job tested; runs nightly in the demo (`jobs.yml`) | `integration/test_privacy_jobs.py::test_retention_deletes_old_tickets_expired_memory_and_old_checkpoints` | pass |
| NFR-14 | Daily budget cap with safe fallback | default $0.50 a day | tests pass | `integration/test_budget_guard.py::test_with_the_budget_at_zero_a_new_ticket_is_handed_off_without_a_model_call`, `::test_a_run_that_reaches_the_budget_half_way_is_handed_off` | pass |
| NFR-15 | Coverage of deterministic safety code | ≥ 95% / 90% | 99.89% of 2,702 lines (`make test`, 2026-11-24, output in the P12-01 pull request); the gate `--cov-fail-under=95` fails the build below 95% (`SAFETY_COVERAGE` in the Makefile) | `make test` | pass |

## Leak scan (2026-11-24)

`make leak-scan` on the local stack after the milestone runs (read-only, no model calls). It scans the app and evaluation databases, the compose logs, the recorded model requests and the run results.

```
leak-scan: scanned database hub, database hub_eval, logs, recordings
  database: hub_eval.checkpoint_writes.blob row (278,6): 2FA
  database: hub_eval.checkpoint_writes.blob row (364,12): PAN
leak-scan: 2 hit(s)
```

Both hits are false positives. Each row is a LangGraph `__interrupt__` write (a 186-byte msgpack blob), and the only text in it that matches is the interrupt's 32-character hex id: one id contains the letters `2fa`, which G1 reads as the 2FA code word, and the other contains a run of 13 or more digits that G1 reads as a card number. No other text in the two blobs matches. The scanner already leaves out SHA-256 digests, channel versions and Slack timestamps for the same reason; it does not yet leave out interrupt ids. That fix is open (a separate task, not part of P12-01).
