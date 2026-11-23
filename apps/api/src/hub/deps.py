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
from hub.observability.tracing import Tracer
from hub.tools.cloud_identity import IdentityToken
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


def identity_token(settings: Settings) -> IdentityToken | None:
    """The Cloud Run ID token for the MCP service, when `MCP_SERVER_AUDIENCE` is set (P10-07)."""
    return IdentityToken(settings.mcp_server_audience) if settings.mcp_server_audience else None


def tool_client(
    settings: Settings, database: Database, tracer: Tracer | None = None
) -> ToolClient | None:
    token = settings.mcp_server_token.get_secret_value()
    if not token:
        return None
    transport = McpTransport(settings.mcp_server_url, token, identity_token(settings))
    return ToolClient(transport, DbToolCallRecorder(database), tracer=tracer)


def build_pipeline(
    settings: Settings, database: Database, retriever: KbSearch | None = None
) -> Pipeline:
    """Gateway, budget guard, active prompts, policy and confidence rule, loaded once: a broken
    config or prompt file stops the start. One guard serves the gateway (every online call)
    and the run (its start).
    `retriever`: the knowledge-base search; without it every run is handed to a human.
    The tool client (P7-04) needs `MCP_SERVER_TOKEN`: without it intake loads no profile and
    research runs no tool loop. Run traces (P9-01) are on when the Langfuse keys are set: one
    tracer serves the gateway, the tool client and the run."""
    budget = BudgetGuard(database, default_limit=Decimal(str(settings.daily_budget_usd)))
    tracer = Tracer.from_settings(settings)
    gateway = build_gateway(settings, DbCallRecorder(database), budget, tracer=tracer)
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
        tools=tool_client(settings, database, tracer),
        policy=load_policy(settings.policy_config),
        confidence_rule=load_confidence(settings.confidence_config),
        retriever=retriever,
        checkpoints=CheckpointStore.postgres(settings.database_url.get_secret_value()),
        auto_reply_allowed=settings.auto_reply_allowed,
        tracer=tracer,
    )


def build_container(settings: Settings) -> Container:
    database = connect(settings.database_url.get_secret_value())
    pipeline = build_pipeline(settings, database)
    kb = build_kb_services(settings, embedder(pipeline.gateway, EMBEDDING_CALL))
    # The search embeds its query through the same gateway the pipeline uses.
    pipeline = dataclasses.replace(pipeline, retriever=kb.retriever)

    identity = identity_token(settings)

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
            "mcp": mcp_check(
                settings.mcp_server_url,
                settings.mcp_server_token.get_secret_value(),
                extra_headers=identity.headers if identity is not None else None,
            ),
        },
    )
