"""The app container: the only shared state of the process (AGENTS.md, code style).

Built once in `create_app` and kept on `app.state.container`. Tests build their own.
"""

import dataclasses
from dataclasses import dataclass, field
from decimal import Decimal

from hub.api.limits import RateLimits
from hub.auth.customer_session import CustomerSessionSigner
from hub.auth.operator_session import OperatorSessionSigner
from hub.config import Settings
from hub.db.session import Database, connect
from hub.graph.checkpoint import CheckpointStore
from hub.graph.confidence import load_confidence
from hub.graph.pipeline import VARIANT, KbSearch, Pipeline, RunTasks, write_prompt_for
from hub.guardrails.policy import load_policy
from hub.kb.service import EMBEDDING_CALL, KbServices, build_kb_services, index_counts
from hub.llm.budget import BudgetGuard
from hub.llm.gateway import build_gateway, embedder
from hub.llm.prompts import active_prompt
from hub.llm.recorder import DbCallRecorder
from hub.observability.readiness import (
    ReadinessCheck,
    kb_index_check,
    mcp_check,
    postgres_check,
    qdrant_check,
)
from hub.tools.mcp_client import DbToolCallRecorder, McpTransport, ToolClient


@dataclass(frozen=True)
class Container:
    settings: Settings
    readiness_checks: dict[str, ReadinessCheck] = field(default_factory=dict)
    # None in tests that need no database.
    database: Database | None = None
    rate_limits: RateLimits = field(default_factory=RateLimits)
    # The workflow graph (P6-01). None in tests that start no
    # run: then a new ticket gets the phase-2 placeholder answer and waits in the queue.
    pipeline: Pipeline | None = None
    runs: RunTasks = field(default_factory=RunTasks)
    # Knowledge-base index and search (P5-01, P5-02). None in tests that need no search: the
    # KB endpoints then answer 503.
    kb: KbServices | None = None

    @property
    def session_signer(self) -> CustomerSessionSigner | None:
        """`None` while SESSION_SECRET is empty: then no customer session can be made or read."""
        secret = self.settings.session_secret.get_secret_value()
        return CustomerSessionSigner(secret) if secret else None

    @property
    def operator_session_signer(self) -> OperatorSessionSigner | None:
        """`None` while SESSION_SECRET is empty: then no operator can log in."""
        secret = self.settings.session_secret.get_secret_value()
        return OperatorSessionSigner(secret) if secret else None


def tool_client(settings: Settings, database: Database) -> ToolClient | None:
    token = settings.mcp_server_token.get_secret_value()
    if not token:
        return None
    return ToolClient(McpTransport(settings.mcp_server_url, token), DbToolCallRecorder(database))


def build_pipeline(
    settings: Settings, database: Database, retriever: KbSearch | None = None
) -> Pipeline:
    """Gateway, budget guard, active prompts, policy and confidence rule, loaded once: a broken
    config or prompt file stops the start. One guard serves the gateway (every online call)
    and the run (its start).
    `retriever`: the knowledge-base search; without it every run is handed to a human.
    The tool client (P7-04) needs `MCP_SERVER_TOKEN`: without it intake loads no profile and
    research runs no tool loop."""
    budget = BudgetGuard(database, default_limit=Decimal(str(settings.daily_budget_usd)))
    gateway = build_gateway(settings, DbCallRecorder(database), budget)
    return Pipeline(
        database=database,
        gateway=gateway,
        budget=budget,
        triage_prompt=active_prompt(settings.prompts_dir, gateway.models, "triage"),
        write_prompt=write_prompt_for(settings.prompts_dir, gateway.models, VARIANT),
        verify_prompt=active_prompt(settings.prompts_dir, gateway.models, "verify"),
        handoff_prompt=active_prompt(settings.prompts_dir, gateway.models, "handoff_summary"),
        memory_prompt=active_prompt(settings.prompts_dir, gateway.models, "memory_summary"),
        research_prompt=active_prompt(settings.prompts_dir, gateway.models, "research"),
        tools=tool_client(settings, database),
        policy=load_policy(settings.policy_config),
        confidence_rule=load_confidence(settings.confidence_config),
        retriever=retriever,
        checkpoints=CheckpointStore.postgres(settings.database_url.get_secret_value()),
        auto_reply_allowed=settings.auto_reply_allowed,
    )


def build_container(settings: Settings) -> Container:
    database = connect(settings.database_url.get_secret_value())
    pipeline = build_pipeline(settings, database)
    kb = build_kb_services(settings, embedder(pipeline.gateway, EMBEDDING_CALL))
    # The search embeds its query through the same gateway the pipeline uses.
    pipeline = dataclasses.replace(pipeline, retriever=kb.retriever)

    async def counts() -> tuple[int, int]:
        return await index_counts(database, kb.index)

    return Container(
        settings=settings,
        database=database,
        pipeline=pipeline,
        kb=kb,
        readiness_checks={
            "postgres": postgres_check(settings.database_url.get_secret_value()),
            "qdrant": qdrant_check(settings.qdrant_url),
            "kb_index": kb_index_check(counts),
            "mcp": mcp_check(settings.mcp_server_url, settings.mcp_server_token.get_secret_value()),
        },
    )
