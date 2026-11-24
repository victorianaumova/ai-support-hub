# Recorded model responses (fake mode)

`LLM_MODE=fake` makes the gateway answer from these files (`hub/llm/gateway.py`, `FixtureProvider`).

- `requests/*.json`: the requests to record. One file per request: `{"role": ..., "messages": [...]}`,
  optionally `"tools"`, or `{"role": "embedding", "texts": [...]}`. Text must already be masked.
  There is no embedding request for now: embeddings come from a local model (ADR-0018), whose
  backend lands in P5-01; until then a real-mode embedding call fails, so it cannot be recorded.
  Optional: `"schema"` (a name from `SCHEMAS` in `hub/llm/record.py`; the request is built as
  `complete_structured` builds it), `"prompt_version"` (written to the `llm_calls` rows), and
  `"models": "primary"` (record only the role's own model, not the fallback).
- `requests/triage_NN.json` are generated from `../triage/examples.jsonl` and the active triage
  prompt by `make llm-requests`; a unit test fails when they are out of date. Do not edit them.
- `requests/run_NN_triage.json` and `requests/run_NN_write.json` are generated from
  `../run/tickets.jsonl` by `make llm-requests` with the same functions the run uses (intake,
  the active prompts, `rag_evidence`), so these tickets work through the API in fake mode
  (integration tests, `make dev`). The write request (`write/v6` since P12; `write/v5`, P7-07; `write/v4`, P7-06; `write/v3`, P7-04; `write/v2`, P5-03)
  holds the six search hits of the ticket, kept in `../run/evidence.json`, and no account facts
  or memory (`(none)`): these runs have no tool client, so they make no research call; `make llm-requests` searches again with the
  real local models (Qdrant in-memory mode, needs `make models`), and an integration test checks
  that the real server finds the same hits. A KB or search change can change the hits; run
  `make llm-requests` and `make record-llm` (about $0.0005 per write request). Only the role's
  own model is recorded.
- `requests/run_NN_verify.json` (P5-04) holds the recorded write answer of a run ticket, so it is
  generated only when that recording exists and the answer passes every code check of the verify
  step (a ticket that cannot answer makes no verify call). After a new write recording, run
  `make llm-requests` again, then `make record-llm`. `requests/verify_*.json` come from
  `../verify/cases.jsonl` (a draft that passes and one with an invented fee).
- `requests/schema_probe.json` checks that the provider accepts every schema shape we use
  (`SchemaProbe`: `const`, a description next to a `$ref`, a nullable `$ref`). First real
  recording 2026-11-16: accepted by Haiku 5.5 and by Sonnet 5.5 with thinking `between_tools`.
- `<key>.json`: one response per model the role uses (model and fallback). The key is a hash of the
  request as the gateway builds it (`ProviderRequest.fixture_key`), so a changed prompt, model or
  parameter needs a new recording. A missing file is an error, never a real call.
- `"recorded": "synthetic"` marks a hand-written response that no model produced. Replace it with
  `make record-llm`: real calls through the gateway (G2 scan, retries, one `llm_calls` row per
  attempt with context `record`); needs `ANTHROPIC_API_KEY` and the database (`make dev`); costs a few cents.
- `make record-llm` skips a model whose fixture already holds a real recording, so a second run
  costs nothing; `make record-llm FORCE=1` records everything again. A changed prompt changes the
  fixture key, so only the changed requests are called. Old fixtures are not deleted.
