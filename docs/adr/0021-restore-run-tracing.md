# ADR-0021: Run tracing with Langfuse is back (P9-01)

- Status: accepted
- Date: 2026-11-23
- Amends: [ADR-0019](0019-scope-cuts.md) (decision 1, "not built: P9-01"). Restores the tracing part of [ADR-0014](0014-observability.md). Owner decision after PR #67.

## Context
ADR-0019 removed phase 9, including P9-01 (Langfuse tracing). Two things changed since then. The operator console (P10-03) was built with a Run tab that UI §3 describes as "steps with duration … link to the trace", and per-step durations had no source: `llm_calls` and `tool_calls` hold calls, not graph nodes. And debugging a bad draft from `runs`, `llm_calls`, `tool_calls` and the audit log means reading four tables without the prompts and answers in between.

## Decision
1. **P9-01 is restored** as one task, built before P10-07 (PR 68). P9-03 (confidence threshold, E9, H7), P9-04 (cost check) and P9-05 (alerts, runbook, `make export-review-candidates`) stay removed as ADR-0019 says.
2. **One trace per run** in Langfuse (Python SDK, OpenTelemetry based): a span per graph node with its duration and outcome, a generation per model call attempt (model, tokens, cost, latency, prompt version, masked messages and answer), a tool observation per tool call (denied ones too), a retriever observation per knowledge-base search (query, chunk ids and scores). The trace id is derived from the run id, so a resume and a recovery add to the same trace; `runs.trace_id` stores it.
3. **Per-step durations are stored on the run** (`runs.steps`, migration 0016), not read from Langfuse. The console's Run tab works with tracing off.
4. **Everything sent passes the G9 filter first** (`hub/observability/tracing.py`): card data masked with the G1 function, any key that names a restriction reason dropped. The same filter is the SDK's `mask` hook. Only that module imports `langfuse` (import-linter contract).
5. **Off without keys.** No `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` means no client, no thread, no network; tests export to memory. Every online run is traced; evaluation runs by a stable hash of the run id below `EVAL_TRACE_SAMPLE`.

## Alternatives considered
- **Durations only, no tracing.** Fills the Run tab, but still no view of prompts and answers when a draft is wrong.
- **LangChain/LangGraph callback handler of Langfuse.** Less code, but it would send the whole graph state of every node, and the G9 filter could not be applied field by field.
- **Read durations from Langfuse for the console.** The console would depend on a vendor API, its limits and its 30-day retention.

## Consequences
- A second sub-processor holds masked support text for 30 days (Langfuse Cloud free plan): security §6 lists what is sent.
- The free plan's 50,000 units per month limit is why evaluation runs are sampled.
- The deploy (P10-07) gets two more optional secrets; H8 may include a Langfuse account, and the demo works without one.
- Model and tool calls outside a run (KB indexing, memory summary on close, the console's Customer tab, evaluation judges) are not traced; `llm_calls` and `tool_calls` still record them.
