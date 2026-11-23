"""Database steps of the workflow: the run row, the ticket's route, the failure path (P6-01;
written for the linear run, P3-05).

The run row is created and committed before the graph starts: the gateway writes `llm_calls`
rows in transactions of their own, and `llm_calls.run_id` refers to `runs.id`.
"""

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select, type_coerce, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from hub.audit import record
from hub.db.models import LlmCall, Run, Ticket
from hub.db.repositories import MessageRepository, TicketRepository
from hub.domain.enums import ActorType, MessageRole, Route, RunStatus, TicketStatus
from hub.graph.context import Step
from hub.graph.pipeline import GRAPH_VERSION, Pipeline

logger = logging.getLogger(__name__)


async def ticket(session: AsyncSession, ticket_id: uuid.UUID) -> Ticket:
    found = await session.get(Ticket, ticket_id)
    if found is None:  # pragma: no cover - tickets are never deleted while a run goes on
        raise LookupError("ticket")
    return found


async def current_status(pipeline: Pipeline, ticket_id: uuid.UUID) -> TicketStatus:
    async with pipeline.database.sessions() as session:
        return (await ticket(session, ticket_id)).status


async def start_run(
    pipeline: Pipeline, ticket_id: uuid.UUID, message_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID] | TicketStatus:
    """The new run's id and the ticket's customer, or the ticket's status when no run starts
    (the ticket is no longer the AI's, or the message is not its own customer message)."""
    async with pipeline.database.sessions.begin() as session:
        found = await ticket(session, ticket_id)
        if found.status != TicketStatus.PROCESSING:
            return found.status
        trigger = await MessageRepository(session).get_in_ticket(ticket_id, message_id)
        if trigger is None or trigger.role != MessageRole.CUSTOMER:
            return found.status
        run = Run(
            ticket_id=ticket_id,
            trigger_message_id=message_id,
            status=RunStatus.RUNNING,
            graph_version=GRAPH_VERSION,
            prompt_versions=pipeline.prompt_versions,
        )
        session.add(run)
        await session.flush()
        run.trace_id = pipeline.tracer.trace_id(run.id, pipeline.context.value)
        return run.id, found.customer_id


async def record_steps(pipeline: Pipeline, run_id: uuid.UUID, steps: Sequence[Step]) -> None:
    """Append one invoke's steps to `runs.steps` (P9-01). Never raises: the steps are for the
    console, and the run's outcome is already settled or settles on its own path."""
    if not steps:
        return
    stored = [step.stored() for step in steps]
    try:
        async with pipeline.database.sessions.begin() as session:
            await session.execute(
                update(Run)
                .where(Run.id == run_id)
                .values(steps=Run.steps.op("||")(type_coerce(stored, JSONB)))
            )
    except Exception as exc:
        logger.warning(
            "run steps not recorded", extra={"run_id": str(run_id), "error": type(exc).__name__}
        )


async def _totals(session: AsyncSession, run_id: uuid.UUID) -> tuple[Decimal, int, int]:
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(LlmCall.cost_usd), 0),
                func.coalesce(func.sum(LlmCall.input_tokens), 0),
                func.coalesce(func.sum(LlmCall.output_tokens), 0),
            ).where(LlmCall.run_id == run_id)
        )
    ).one()
    return Decimal(row[0]), int(row[1]), int(row[2])


async def finish_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    status: RunStatus,
    route: Route,
    reasons: list[str],
    models: dict[str, str],
    error: str | None = None,
    confidence: float | None = None,
    features: dict[str, float] | None = None,
) -> None:
    run = await session.get(Run, run_id)
    if run is None:  # pragma: no cover - the row was committed in `start_run`
        return
    run.total_cost_usd, run.input_tokens, run.output_tokens = await _totals(session, run_id)
    run.status = status
    run.route = route
    run.route_reasons = reasons
    run.models = models
    run.error = error
    run.confidence = to_decimal(confidence)
    run.features = dict(features or {})
    # A paused run (`interrupted`) has its outcome so far, but has not finished.
    run.finished_at = None if status == RunStatus.INTERRUPTED else datetime.now(UTC)


def to_decimal(value: float | None) -> Decimal | None:
    """A confidence for a `numeric(4,3)` column."""
    return None if value is None else Decimal(str(round(value, 3)))


async def audit_route(
    session: AsyncSession,
    pipeline: Pipeline,
    ticket_id: uuid.UUID,
    run_id: uuid.UUID | None,
    route: Route,
    reasons: list[str],
    confidence: float | None = None,
    features: dict[str, float] | None = None,
) -> None:
    """`route.decided` with the reasons, the confidence and its features (set by `decide`; none
    for a route of `policy_gate` or a failure), and the versions (security §5)."""
    await record(
        session,
        actor_type=ActorType.SYSTEM,
        actor_id=None,
        action="route.decided",
        entity_type="ticket",
        entity_id=ticket_id,
        details={
            "run_id": run_id,
            "route": route.value,
            "reasons": reasons,
            "confidence": confidence,
            "features": dict(features or {}),
            "graph_version": GRAPH_VERSION,
            "prompt_versions": pipeline.prompt_versions,
        },
    )


async def hand_off(
    session: AsyncSession,
    pipeline: Pipeline,
    ticket_id: uuid.UUID,
    run_id: uuid.UUID | None,
    reasons: list[str],
    confidence: float | None = None,
    features: dict[str, float] | None = None,
) -> TicketStatus:
    """The ticket goes to a human with `reasons`, unless a human already has it (an operator
    claimed it while the run worked). Returns the ticket's status."""
    repository = TicketRepository(session)
    found = await repository.lock(await ticket(session, ticket_id))
    if found.status == TicketStatus.PROCESSING:
        found.last_route = Route.HANDOFF
        found.route_reasons = reasons
        found.confidence = to_decimal(confidence)
        await repository.change_status(found, TicketStatus.HANDED_OFF)
        await audit_route(
            session, pipeline, ticket_id, run_id, Route.HANDOFF, reasons, confidence, features
        )
    return found.status


async def fail(
    pipeline: Pipeline,
    ticket_id: uuid.UUID,
    run_id: uuid.UUID | None,
    error: str,
    models: dict[str, str],
    reasons: list[str],
) -> TicketStatus:
    """The last resort when the graph itself failed (a node that does not fail into a handoff,
    the checkpointer, the database): the run is `failed` and the ticket goes to a human. When
    the ticket already left `processing` (e.g. the draft was stored and then `respond` failed),
    the run's route is the one the ticket has, so `runs.route` and `tickets.last_route` agree."""
    async with pipeline.database.sessions.begin() as session:
        status = await hand_off(session, pipeline, ticket_id, run_id, reasons)
        if run_id is not None:
            found = await ticket(session, ticket_id)
            await finish_run(
                session,
                run_id,
                status=RunStatus.FAILED,
                route=found.last_route or Route.HANDOFF,
                reasons=list(found.route_reasons or reasons),
                models=models,
                error=error,
            )
        return status
