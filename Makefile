# Commands from docs/operations.md §1. Targets that are not built yet print
# "not implemented" and exit with an error, so nobody mistakes a stub for a passing step.
# When you implement a target, move it from NOT_IMPLEMENTED to BUILT: the unit tests check
# (in make's database, without running anything) that every NOT_IMPLEMENTED target is the stub.
# Do not use $(shell), $(eval) or include here: make runs them while reading the file.

SHELL := /bin/bash
.DEFAULT_GOAL := help

BUILT := setup lint test data-check kb-numbers up down dev test-int migrate seed kb-load \
	record-llm llm-requests eval eval-attacks leak-scan models mcp mcp-smoke retention web-types
NOT_IMPLEMENTED := test-e2e \
	eval-smoke eval-report \
	export-review-candidates rollback

.PHONY: eval-rejudge help $(BUILT) $(NOT_IMPLEMENTED)

help:
	@echo "Built:           $(BUILT)"
	@echo "Not implemented: $(NOT_IMPLEMENTED)"

setup:
	uv sync --all-packages --locked
	pnpm -C apps/web install --frozen-lockfile
	@if [ ! -f .env ]; then cp .env.example .env && echo "created .env from .env.example"; fi
	uv run pre-commit install
	$(MAKE) --no-print-directory models

# The local embedding model and the BM25 files (ADR-0018), into EMBEDDING_CACHE_DIR. Needs the
# network once; the app loads them with local_files_only and never downloads. The model file is
# checked against its pinned SHA-256 (hub/llm/local_embedding.py).
models:
	uv run python -m hub.llm.local_embedding

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	uv run lint-imports --no-logo
	pnpm -C apps/web lint
	pnpm -C apps/web typecheck

# Coverage gate for deterministic safety code (AGENTS.md, security §3): >= 95% line coverage.
# hub.domain holds the ticket status transitions and the seed rules; hub.audit.chain the audit
# hash chain and the masking of audit details; hub.auth the password hashing, the operator
# session and CSRF token, the role rules and the login lockout; hub.llm.gateway the outbound card
# scan (G2), retries and fallback; hub.llm.pricing the cost of every call; hub.guardrails.policy,
# hub.graph.routing and hub.graph.confidence the routing decision (P6-02); hub.tools.permissions
# the tool permissions (G6, P7-03).
SAFETY_COVERAGE := --cov=hub.guardrails --cov=hub.domain --cov=hub.audit.chain --cov=hub.auth \
	--cov=hub.llm.gateway --cov=hub.llm.structured --cov=hub.llm.pricing --cov=hub.llm.budget \
	--cov=hub.graph.routing --cov=hub.graph.confidence --cov=hub.tools.permissions \
	--cov-report=term-missing:skip-covered --cov-fail-under=95

# Python unit tests, then the web app's Vitest tests (P10-01). The CI `test` job has no Node; its
# `web` job runs the Vitest tests, so the web step is skipped where `make setup` did not install.
test:
	uv run pytest $(SAFETY_COVERAGE)
	@if [ -d apps/web/node_modules ]; then pnpm -C apps/web test; else echo "web: no node_modules (run make setup); Vitest skipped"; fi

# TypeScript types of the API for the web app (api.md: generated, never written by hand). The
# OpenAPI document comes from the app object itself, so no server, database or .env is needed.
# CI regenerates them and fails when the committed file differs.
web-types:
	uv run python -m hub.openapi > apps/web/openapi.json
	pnpm -C apps/web types

data-check:
	uv run python -m hub_evals.data_check data

# Report, not a gate (P2-08): numbers in data/kb/ that are not in the world file. The list must be
# empty or each item explained in the pull request.
kb-numbers:
	uv run python -m hub_evals.kb_numbers data/kb docs/tallybridge-world.md

COMPOSE := docker compose -f infra/docker-compose.yml

# --wait returns when every service is healthy (the api healthcheck uses /health).
up:
	$(COMPOSE) up --detach --build --wait

down:
	$(COMPOSE) down

# Alembic upgrade as the migrator (DATABASE_URL_DIRECT), then the evaluation database hub_eval
# (EVAL_DATABASE_URL_DIRECT, P4-03): default privileges are per database, so the app role gets
# its grants there only from the migrations. The LangGraph checkpointer setup (operations §1)
# joins here in phase 6.
migrate:
	uv run alembic -c infra/alembic.ini upgrade head
	uv run alembic -c infra/alembic.ini -x db=eval upgrade head

# Demo customers, operators (password from SEED_OPERATOR_PASSWORD) and default settings, as the
# app role (DATABASE_URL). Idempotent: a second run changes nothing.
seed:
	uv run python -m hub.seed data/seed

# Knowledge base into Postgres (kb_documents, kb_chunks) as the app role: only changed files are
# written, deleted files are removed. Then the Qdrant index is synced with Postgres (P5-01): only
# chunks whose text changed are embedded again. FULL=1 builds a new collection and moves the alias.
kb-load:
	uv run python -m hub.kb.service $(if $(FULL),--full,) data/kb

# operations §1: dev = models + up + migrate + seed + kb-load (models first: the api container
# mounts the model folder, and a cached model makes no network call).
# The check runs first and alone: with required settings empty (SEED_OPERATOR_PASSWORD,
# SESSION_SECRET, DEMO_ACCESS_CODE) it lists them all and stops before anything starts. The
# steps run through a sub-make, in this order, so the check always comes before them.
dev:
	uv run python -m hub.dev_check
	$(MAKE) --no-print-directory models up migrate seed kb-load
	@echo "dev: stack is up. API http://127.0.0.1:8000 (/health, /ready); MCP server http://127.0.0.1:8001/mcp"

# The support-tools MCP server over stdio, the way Claude Code starts it (README, P7-01).
mcp:
	uv run python -m support_tools --transport stdio

# Lists the tools and calls one core tool with the official MCP client, over streamable HTTP
# (bearer token) and over stdio; both servers are started as subprocesses (mock data only).
mcp-smoke:
	uv run python -m support_tools.smoke

# Integration tests start their own Postgres and Qdrant with testcontainers (needs Docker).
test-int:
	uv run pytest apps/api/tests/integration

# Real model calls through the gateway (G2 scan, retries, llm_calls rows with context `record`):
# needs ANTHROPIC_API_KEY in .env and the database (`make dev`); costs money.
# Rewrites the recorded responses that fake mode (LLM_MODE=fake) serves to the tests, then the
# recorded judge responses of evals/tests/fixtures/llm (P4-05).
record-llm:
	uv run python -m hub.llm.record $(if $(FORCE),--force,)
	uv run python -m hub_evals.record $(if $(FORCE),--force,)

# Rewrites tests/fixtures/llm/requests/triage_*.json from the triage examples and run_*.json from
# the run tickets (their search hits again with the local models: needs `make models`), and
# evals/tests/fixtures/llm/requests/judge_*.json from the judge test cases,
# with the active prompts and data/kb (no model call). Run it after a prompt or KB change, then
# `make record-llm` (a KB change changes every write request).
llm-requests:
	uv run python apps/api/tests/unit/_triage_requests.py
	uv run python apps/api/tests/unit/_run_requests.py --search
	uv run python evals/tests/_judge_requests.py

# The evaluation runner (eval plan §4) on the 20 dev tickets, against EVAL_DATABASE_URL (run
# `make migrate` first). Real models cost money: by hand, only when the owner asks (ADR-0018);
# `LLM_MODE=fake make eval LIMIT=10` checks the runner for free. The default variant rag_hybrid
# (and rag_dense) also needs Qdrant (`make up`) and the local models (`make models`): the runner
# indexes the evaluation KB under its own alias, never the app's. Options: VARIANT=<name>,
# LIMIT=<n> (first n tickets), NO_CACHE=1 (milestone run), RESTART=1 (run every ticket again),
# RUN_DIR=<dir> (resume that run), NO_JUDGES=1 (skip the LLM judges).
eval:
	uv run python -m hub_evals.runner $(if $(VARIANT),--variant $(VARIANT),) \
		$(if $(LIMIT),--limit $(LIMIT),) $(if $(NO_CACHE),--no-cache,) \
		$(if $(RESTART),--restart,) $(if $(RUN_DIR),--run-dir $(RUN_DIR),) \
		$(if $(NO_JUDGES),--no-judges,) $(if $(NO_TOOLS),--no-tools,)

# The 16 attacks through the same workflow as `make eval`, with the success checks of eval plan
# §3 and the free G4/G1 probes (P8-01). Real models cost money: by hand, when the owner asks.
# ESTIMATE=1 prints the cost estimate and runs nothing; PROBES=1 only the probes (no database).
eval-attacks:
	uv run python -m hub_evals.attacks $(if $(NO_CACHE),--no-cache,) \
		$(if $(RUN_DIR),--run-dir $(RUN_DIR),) $(if $(NO_TOOLS),--no-tools,) \
		$(if $(ESTIMATE),--estimate-only,) $(if $(PROBES),--probes-only,)

# After an eval run (P8-03): card data and canary strings in both databases, the stack's logs and
# the recorded model requests and results. Prints locations only, never the text; exit 1 on a hit.
leak-scan:
	$(COMPOSE) logs --no-color 2>/dev/null | uv run python -m hub_evals.leak_scan --logs -

# The retention job once (P8-05), through the running local API (`make dev`) like the nightly
# schedule: POST /admin/jobs/retention with JOBS_TOKEN from .env (read, not sourced).
retention:
	@token="$$(grep -E '^JOBS_TOKEN=' .env 2>/dev/null | tail -1 | cut -d= -f2-)"; \
	if [ -z "$$token" ]; then echo "make retention: set JOBS_TOKEN in .env"; exit 1; fi; \
	curl -fsS -X POST -H "Authorization: Bearer $$token" \
		"$${API_URL:-http://localhost:8000}/api/v1/admin/jobs/retention"; echo

# Judge a finished RAG run again with the active judge prompts (judge calls only, P7-06):
# RUN=evals/runs/<dir> writes <dir>-rejudged. Real models cost money: by hand.
eval-rejudge:
	uv run python -m hub_evals.rejudge $(RUN)

$(NOT_IMPLEMENTED):
	@echo "make $@: not implemented"
	@exit 1
