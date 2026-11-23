# Enterprise AI Support Hub

AI customer support for **Tallybridge**, a payments platform for freelancers and small businesses. A LangGraph workflow answers routine tickets in seconds, with a source for every fact, and sends everything risky to a human operator with a prepared draft.

> Tallybridge is a fictional company made for this project. It has no relation to any real company. All data is synthetic.

**Status:** build in progress (phase 4, evaluation). This README is replaced by the final version in phase 12 (demo link, numbers, diagram).

## What the system does

1. A customer writes in the chat. Card numbers are masked before anything is stored.
2. The workflow classifies the ticket, searches the knowledge base, reads the customer's own account data through tools, writes an answer with citations and checks it.
3. Fixed policy rules and a rule-based confidence score (uncalibrated, ADR-0018) choose a route: **auto-reply**, **operator approval** or **handoff**.
4. Operators review drafts in a console. A support lead sees quality, cost and automation numbers and can switch auto-replies off.
5. Every model call is traced and costed. Every decision is in an audit log. Quality is measured on labelled tickets before anything is automated.

## Documents

| Document | Question it answers |
|---|---|
| [docs/prd.md](docs/prd.md) | What do we build, for whom, and how do we know it works? |
| [docs/tallybridge-world.md](docs/tallybridge-world.md) | What are the facts of the fictional company? |
| [docs/design.md](docs/design.md) | How is the system built? Start here for architecture. |
| [docs/data-model.md](docs/data-model.md) | Which tables, index and files exist? |
| [docs/api.md](docs/api.md) | Which endpoints, events and MCP tools exist? |
| [docs/ai-pipeline.md](docs/ai-pipeline.md) | How does the workflow think: nodes, prompts, routing, memory? |
| [docs/eval-plan.md](docs/eval-plan.md) | How is quality measured, with which targets? |
| [docs/security.md](docs/security.md) | What can go wrong and what stops it? |
| [docs/operations.md](docs/operations.md) | How to run, observe, test and deploy? |
| [docs/ui.md](docs/ui.md) | What do the chat and the console look like? |
| [docs/adr/](docs/adr/README.md) | Why was each key decision made? |
| [docs/build-plan.md](docs/build-plan.md) | In which order is it built, task by task? |
| [docs/build-process.md](docs/build-process.md) | How is the build organised: sessions, agent roles, reviews, what the owner does? |
| [AGENTS.md](AGENTS.md) | Rules for coding agents. |

## Technology

Python 3.13 · FastAPI · LangGraph · LiteLLM (Anthropic only) · FastEmbed (local embeddings) · Qdrant (hybrid search) · Postgres · MCP · Langfuse · React + TypeScript · Docker · GitHub Actions · Cloud Run.

**Budget mode** ([ADR-0018](docs/adr/0018-budget-mode.md)): the whole project spends about $5 on model calls. Every model role runs on Claude Haiku 5.5 (fallback Claude Sonnet 5.5); embeddings come from a local model. The LLM judge is the same model family as the writer, so it may prefer the writer's answers; judge numbers are reported as unvalidated. Evaluation uses 20 tickets and 16 attacks: a demo check that the system works, not a proof of quality ([eval plan](docs/eval-plan.md)).

## First numbers (phase 4, baseline B0)

> **Synthetic data. Demo check, not a proof.** 20 synthetic tickets that are also the tuning set; one ticket is 5 points, and 0 errors in 20 still allows a true error rate of up to 16%. Metrics marked *judged* come from an LLM judge that is **unvalidated and the same model family** as the writer (Claude Haiku 5.5).

Run `2026-11-17-b0_full_kb-4bc48fc`: the phase 3 linear pipeline (whole knowledge base in the prompt, no retrieval, no routing yet). Full report: [evals/runs/2026-11-17-b0_full_kb-4bc48fc/report.md](evals/runs/2026-11-17-b0_full_kb-4bc48fc/report.md); error analysis: [error-analysis.md](evals/runs/2026-11-17-b0_full_kb-4bc48fc/error-analysis.md); history: [docs/experiments.md](docs/experiments.md).

| Metric | Value (count, 95% Wilson interval) | Target | Red line | Result |
|---|---|---|---|---|
| Triage accuracy | 17 of 20 (64%–95%) | ≥ 18 of 20 | 16 of 20 | below target |
| Flag recall (3 flags, n = 1 each) | 1 of 1 each (21%–100% each) | all | all | pass |
| Key-fact coverage *(judged)* | 0.61 (14 tickets) | ≥ 0.85 | 0.75 | red line missed |
| Answered tickets with an unsupported claim *(judged)* | 12 of 13 (67%–99%) | ≤ 1 | 2 | red line missed |
| Answered tickets with an invalid citation *(judged)* | 10 of 13 (50%–92%) | 0 | 1 | red line missed |
| Hard-handoff recall | 0 of 8 (0%–32%) | 1.00 | 1.00 | red line missed (no routing before phase 6) |
| Escalation recall | 10 of 10 (72%–100%) | 1.00 | 1 miss | pass (trivial: everything goes to approval) |
| Wrong auto-reply | 0 of 0 (auto-reply is off) | 0 | any | pass (trivial: everything goes to approval) |
| Cost per ticket (answers only) | $0.0015; $0.0024 with judges ($0.048 for the run) | ≤ €0.05 | €0.10 | pass |
| Retrieval, latency of auto-replies, safety | not measured yet (phases 5, 6 and 8) | | | — |

Every ticket went to operator approval: B0 has no policy gate or handoff yet, so the hard-handoff red line cannot be met until P6-02, and the escalation-recall and wrong-auto-reply passes say nothing about routing quality or safety. Auto-reply stays off.

## Phase 5 numbers (retrieval, `rag_hybrid`)

> Same caveats as above: **synthetic data, demo check, not a proof**; *judged* metrics are unvalidated, same model family.

Run `2026-11-17-rag_hybrid-4fc85cb`: the linear pipeline with one hybrid knowledge-base search (top 6 chunks), the writer `write/v2` citing chunks, and the new verify step (code checks, then an LLM claim check). Full report: [evals/runs/2026-11-17-rag_hybrid-4fc85cb/report.md](evals/runs/2026-11-17-rag_hybrid-4fc85cb/report.md); error analysis: [error-analysis.md](evals/runs/2026-11-17-rag_hybrid-4fc85cb/error-analysis.md).

**B0 against RAG is not measured.** The comparison experiment (E1) was removed in budget mode (ADR-0018), and the faithfulness and citation numbers are not comparable for two reasons: this run was judged with `judge_*/v2` and B0 with v1, and the judges' evidence differs (here every chunk the writer read; in B0 only the documents the draft cites).

| Metric | Value (count, 95% Wilson interval) | Gate 5 / red line | Result |
|---|---|---|---|
| hit@5 (first search) | 14 of 18 (55%–91%); MRR 0.61 | ≥ 0.80 | **missed** (one ticket short) |
| Answered tickets with an unsupported claim *(judged)* | 6 of 9 (35%–88%) | at most 2 | **missed** |
| Key-fact coverage *(judged)* | 0.41 (14 tickets) | 0.75 | missed |
| Answered tickets with an invalid citation *(judged)* | 5 of 9 (27%–81%) | 1 | missed |
| Drafts that answered (`can_answer`) | 9 of 20 | — | reported |
| Cost per ticket (answers only) | $0.0007; $0.018 paid for the run with judges | €0.10 | pass |

Gate 5 is not met. The error analysis sorts every failure by cause. The prompts were not tuned to these numbers; changes are decided by the owner. Every ticket still goes to operator approval, and auto-reply stays off.

## Load smoke (phase 10, fake models)

**Fake models: model time not included.** `make load-smoke` (P10-08, `infra/load/smoke.py`): 10 parallel demo customers send 50 tickets through the API and the whole workflow (triage, search, write, verify) with recorded model answers, so it measures the service around the models, not answer time. Run on 2026-11-23 against the local e2e stack (`make test-e2e KEEP=1`: one API container, Postgres, Qdrant, local embeddings on a laptop), not a cloud deployment (P10-07 is still waiting for human gate H8).

| Tickets | Latency to the stream's `done` p50 | p95 | Error rate | Lost runs |
|---|---|---|---|---|
| 50 (10 customers × 5) | 1,423 ms | 1,915 ms | 0 of 50 (0%) | 0 |

## Connect to Claude Code

The support-tools MCP server (`apps/mcp_server`, [API](docs/api.md) §7) works from any MCP client. It serves mock data only: the 40 fictional customers of `data/seed`, and Jira and Slack mocks that store calls in a local SQLite file.

**Over stdio** (Claude Code starts the server itself; run from the repository root after `make setup`):

```
claude mcp add --env CORE_ANCHOR_DATE=2026-11-12 --transport stdio support-tools -- uv run --directory "$PWD" python -m support_tools --transport stdio
```

Leave out `--env CORE_ANCHOR_DATE=…` to use today's date. Keep `--transport stdio` between `--env` and the name.

**Over HTTP** (the server of `make dev`, port 8001, with the token from `.env`):

```
claude mcp add --transport http support-tools http://127.0.0.1:8001/mcp --header "Authorization: Bearer $MCP_SERVER_TOKEN"
```

Check with `claude mcp list`, or `/mcp` inside a session, then ask for example "Use core_get_transaction for CUS-0001, TX-000900". Without `--scope` the server is added for this project on your machine only (`local`). `make mcp-smoke` checks both transports with the official MCP client (list the tools, call `core_get_transaction`). The MCP Inspector works the same way: `npx @modelcontextprotocol/inspector@2.13.0 uv run python -m support_tools`.

## How this project is built

The design package was written first. A coding agent builds the system phase by phase from [the build plan](docs/build-plan.md); the owner reviews each phase, checks the attack set and makes the risk decisions (whether auto-reply goes on in the demo). Each phase ends with a walkthrough for the owner, written locally to `docs/walkthrough/`. Walkthroughs are personal notes: the folder is git-ignored and they are not in this repository.

## CI and branch protection

CI (`.github/workflows/ci.yml`, see [operations](docs/operations.md) §5) runs on every pull request and every push to `main`. It has four required jobs: `lint` (ruff, mypy strict, import-linter), `test` (unit tests, with no provider API key), `data-check`, and `security` (gitleaks over the whole history, pip-audit). Each runs the same `make` target you run locally. A fifth job, `hook-bootstrap`, is **informational and not required**: it shows that the gitleaks pre-commit hook installs on a machine with no Go and no pre-commit cache. It downloads Go from go.dev, so an outage there must not block a merge.

Branch protection is **not turned on yet**. GitHub does not offer branch protection or rulesets for a private repository on the free plan, and this repository stays private until phase 12. Until then the rules below hold **by process**: the coding agent opens pull requests and never merges, and only the owner merges, after CI is green.

The owner turns branch protection on in phase 12, when the repository becomes public (Settings → Rules → Rulesets, or Settings → Branches). The rule for `main`:

- Changes reach `main` only through a pull request. Nobody pushes to `main` directly, admins included.
- Required status checks: `lint`, `test`, `data-check`, `security` (not `hook-bootstrap`). The branch must be up to date with `main` before merging.
- Every review conversation must be resolved before merging.
- No force-pushes, and `main` cannot be deleted.
- One task = one pull request (see [build process](docs/build-process.md)). The coding agent opens it; only the owner merges.

## Honest limits

- Synthetic company, synthetic customers, synthetic tickets. Numbers show that the method works, not how a real support team would perform.
- Designed with PCI-DSS- and GDPR-inspired constraints (card-number masking, data minimisation, audit log). Not certified.
- English only. One channel (chat).
