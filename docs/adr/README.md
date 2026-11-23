# Architecture Decision Records

One file per decision. To change a decision, add a new ADR that replaces the old one and set the old status to `superseded by ADR-XXXX`. Do not edit the decision of an accepted ADR.

| # | Decision |
|---|---|
| [0001](0001-modular-monolith.md) | Modular monolith, not microservices |
| [0002](0002-workflow-with-bounded-agentic-step.md) | Fixed workflow with one bounded agentic step (LangGraph) |
| [0003](0003-litellm-provider-gateway.md) | LiteLLM as the gateway to several model providers |
| [0004](0004-structured-output.md) | Structured output with provider JSON schema and Pydantic validation |
| [0005](0005-qdrant-hybrid-search.md) | Qdrant with hybrid search (dense + BM25, RRF) |
| [0006](0006-section-chunking.md) | Chunk knowledge-base articles by section |
| [0007](0007-in-request-processing-sse.md) | Process a ticket inside the HTTP request and stream progress with SSE |
| [0008](0008-hitl-interrupt-checkpointer.md) | Human-in-the-loop with LangGraph interrupt and a Postgres checkpointer |
| [0009](0009-confidence-and-routing.md) | Routing by policy rules first, then a calibrated confidence model |
| [0010](0010-masking-before-everything.md) | Mask card data before storage and before any model call |
| [0011](0011-mcp-tools-and-identity-binding.md) | All external tools behind one MCP server; customer identity injected by the Hub |
| [0012](0012-customer-memory.md) | Customer memory as ticket summaries in Postgres |
| [0013](0013-prompts-as-files.md) | Prompts are versioned files in the repository |
| [0014](0014-observability.md) | Tracing with Langfuse plus an own `llm_calls` table |
| [0015](0015-free-tier-deployment.md) | Free-tier deployment: Cloud Run + Neon + Qdrant Cloud |
| [0016](0016-web-ui-and-auth.md) | React console served by the API; cookie sessions |
| [0017](0017-ibans-always-partly-masked.md) | IBANs are always partly masked (replaces part of 0010) |
| [0018](0018-budget-mode.md) | Budget mode: Anthropic only, local embeddings, small evaluation (replaces parts of 0003, 0005, 0009, 0013) |
| [0019](0019-scope-cuts.md) | Scope cuts: phase 9 removed, one review round per pull request, Jira and Slack mock only (replaces parts of 0014, 0018) |
| [0020](0020-exact-error-code-lookup.md) | Exact error-code lookup in hybrid search (adds to 0005) |
| [0021](0021-restore-run-tracing.md) | Run tracing with Langfuse is back, P9-01 only (amends 0019, restores the tracing part of 0014) |

Template: Context → Decision → Alternatives considered → Consequences. Half a page.
