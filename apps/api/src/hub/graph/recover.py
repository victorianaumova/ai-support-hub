"""Recovery of runs stuck in `running` (P6-03; design §10, api.md `POST /admin/runs/recover`).

A run is stuck when its process died (a restart, a deploy, an instance that went away) while it
worked: the run row says `running`, but nobody runs it. The state is checkpointed after every
node, so recovery continues the run from its last checkpoint; a run that died before its first
checkpoint starts again from its trigger message with the same run id. The nodes are safe to run
again (ADR-0008): `human_review` stores a run's draft once, a ticket moves only from
`processing`, and a handoff notice is sent only when the ticket was handed off by that node.

**When a run counts as stuck (owner decision, P6-03).** It started longer ago than any run can
take (`STUCK_AFTER_S`: the longest path at every node's timeout, the retry loop included, plus a
margin), and this process does not run it (`Pipeline.live`). A run on the other instance (Cloud
Run, at most 2) has ended or failed by then, so a recovery never runs a live run a second time.
design §10 named 90 s; a slow run (model retries and fallbacks) can take longer than that.

Two ways in: `POST /admin/runs/recover` (all stuck runs) and the next read of the ticket (the
customer's ticket and messages, the console ticket), which starts the recovery as a task of its
own and does not wait for it. A recovery that fails is the run's last resort: the run is
`failed` and the ticket goes to a human (`system_error`). Every recovery writes `run.recovered`.

**A lost resume (P8-05).** The console commits the operator's decision and resumes the paused
run afterwards (ADR-0008). When the process stops between the two, the answer is sent but the
run stays `interrupted`: its row is never finished and the approved actions of P7 never run.
`recover_runs` finds such a run (an `interrupted` run whose ticket no longer waits for it,
decided longer ago than `STUCK_AFTER_S`, not live here), rebuilds the `ReviewDecision` from our
own tables - the draft's review and its `draft.<status>` audit record, or the first operator
`message.sent` / `ticket.closed` on the ticket since the run started - and resumes it.
The checkpoint is never read for the decision (ADR-0008: the console reads our tables only).
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hub.audit import record
from hub.db.models import AuditLog, Draft, Message, Run, Ticket
from hub.domain.enums import ActorType, DraftStatus, RunStatus
from hub.domain.review import ReviewDecision, ReviewKind
from hub.graph.build import LONGEST_RUN_S, compile_graph
from hub.graph.context import GraphContext
from hub.graph.nodes.human_review import WAITING
from hub.graph.pipeline import Pipeline
from hub.graph.run import (
    RunIncompleteError,
    ignore,
    invoke,
    outcome_status,
    paused_at,
    resume_run,
    settle_failure,
    thread,
)
from hub.graph.state import TicketState, run_input

logger = logging.getLogger(__name__)

# Longer than any run: every node at its timeout, one after the other, the retry loop's nodes
# twice (P6-04), and a margin for the database steps between them.
STUCK_AFTER_S = LONGEST_RUN_S + 60.0


@dataclass(frozen=True)
class StuckRun:
    run_id: uuid.UUID
    ticket_id: uuid.UUID
    message_id: uuid.UUID | None
    customer_id: uuid.UUID


@dataclass(frozen=True)
class Recovery:
    """Who asked: an admin (`operator`), the `recover-runs` job (`system`, actor `jobs`) or the
    next read of a ticket (`system`, no actor)."""

    actor_type: ActorType = ActorType.SYSTEM
    actor_id: uuid.UUID | str | None = None


@dataclass(frozen=True)
class RecoverReport:
    """`found`, `resumed` and `failed` count stuck runs and lost resumes together; `lost` is the
    number of lost resumes among `found`."""

    found: int
    resumed: int
    failed: int
    lost: int = 0


@dataclass(frozen=True)
class LostResume:
    run_id: uuid.UUID
    ticket_id: uuid.UUID
    decision: ReviewDecision


# A reviewed draft, by the status the review gave it (console_drafts `_KIND`).
_REVIEWED: dict[DraftStatus, ReviewKind] = {
    DraftStatus.APPROVED: "approve",
    DraftStatus.EDITED: "edit",
    DraftStatus.REJECTED: "reject",
}
# What resumes a handoff's run: the first reply, or a close without one (console.py).
_HANDOFF_DECISIONS = ("message.sent", "ticket.closed")


async def stuck_runs(
    session: AsyncSession, pipeline: Pipeline, *, ticket_id: uuid.UUID | None = None
) -> list[StuckRun]:
    """Runs in `running` that started more than `STUCK_AFTER_S` ago (database time) and are not
    live in this process; oldest first."""
    statement = (
        select(Run.id, Run.ticket_id, Run.trigger_message_id, Ticket.customer_id)
        .join(Ticket, Ticket.id == Run.ticket_id)
        .where(
            Run.status == RunStatus.RUNNING,
            Run.started_at < func.now() - timedelta(seconds=STUCK_AFTER_S),
        )
        .order_by(Run.started_at, Run.id)
    )
    if ticket_id is not None:
        statement = statement.where(Run.ticket_id == ticket_id)
    rows = (await session.execute(statement)).all()
    return [StuckRun(*row) for row in rows if row[0] not in pipeline.live]


async def recover_runs(
    pipeline: Pipeline, recovery: Recovery, *, ticket_id: uuid.UUID | None = None
) -> RecoverReport:
    """Recover every stuck run (of one ticket, with `ticket_id`), one after the other. Without
    `ticket_id` (the admin endpoint, the `recover-runs` job) also every lost resume."""
    async with pipeline.database.sessions() as session:
        found = await stuck_runs(session, pipeline, ticket_id=ticket_id)
        lost = [] if ticket_id is not None else await lost_resumes(session, pipeline)
    resumed = failed = 0
    for stuck in found:
        ended = await recover_run(pipeline, stuck, recovery)
        if ended is None:
            continue  # taken by another recovery in this process meanwhile
        resumed += ended
        failed += not ended
    for item in lost:
        ended = await resume_lost(pipeline, item, recovery)
        if ended is None:
            continue
        resumed += ended
        failed += not ended
    return RecoverReport(
        found=len(found) + len(lost), resumed=resumed, failed=failed, lost=len(lost)
    )


async def lost_resumes(session: AsyncSession, pipeline: Pipeline) -> list[LostResume]:
    """`interrupted` runs whose operator decision was committed more than `STUCK_AFTER_S` ago
    (database time) but never resumed them, with the decision rebuilt; oldest first. A run whose
    decision cannot be rebuilt is left alone (logged): it still waits, as before P8-05."""
    rows = (
        await session.execute(
            select(Run.id, Run.ticket_id, Run.started_at)
            .where(Run.status == RunStatus.INTERRUPTED)
            .order_by(Run.started_at, Run.id)
        )
    ).all()
    now = (await session.execute(select(func.now()))).scalar_one()
    found: list[LostResume] = []
    for run_id, ticket_id, started_at in rows:
        if run_id in pipeline.live:
            continue
        rebuilt = await rebuild_decision(session, run_id, ticket_id, started_at)
        if rebuilt is None:
            continue
        decision, decided_at = rebuilt
        if (now - decided_at).total_seconds() <= STUCK_AFTER_S:
            continue  # the console's own resume may still be on its way
        found.append(LostResume(run_id=run_id, ticket_id=ticket_id, decision=decision))
    return found


async def rebuild_decision(
    session: AsyncSession, run_id: uuid.UUID, ticket_id: uuid.UUID, started_at: datetime
) -> tuple[ReviewDecision, datetime] | None:
    """The decision that should have resumed the paused run, and when it was committed; None
    while the run still waits (or the decision cannot be rebuilt). Built from the draft, the
    ticket and the audit log, never from the checkpoint or a model."""
    found_draft = await session.execute(select(Draft).where(Draft.run_id == run_id))
    draft = found_draft.scalar_one_or_none()
    if draft is not None and draft.status == DraftStatus.PENDING:
        return None
    if draft is not None and draft.status in _REVIEWED:
        return await _from_review(session, draft)
    found = await session.get(Ticket, ticket_id)
    if found is None or found.status in WAITING:
        return None
    return await _from_handoff(session, ticket_id, started_at)


async def _from_review(
    session: AsyncSession, draft: Draft
) -> tuple[ReviewDecision, datetime] | None:
    action = f"draft.{draft.status.value}"
    row = await _first_audit(session, action, entity_type="draft", entity_id=draft.id)
    if row is None or draft.reviewed_by is None:
        logger.warning("lost resume not rebuilt", extra={"draft_id": str(draft.id)})
        return None
    message_id = row.details.get("message_id")
    kind = _REVIEWED[draft.status]
    decision = ReviewDecision(
        kind=kind,
        operator_id=str(draft.reviewed_by),
        # As the console built it: the operator's own text; an approve sends the draft's text.
        body=None if kind == "approve" else draft.final_body,
        # Null for a review before migration 0013: "no actions".
        approved_actions=[str(action_id) for action_id in draft.approved_actions or []],
        reject_reason=None if draft.reject_reason is None else draft.reject_reason.value,
        message_id=None if message_id is None else str(message_id),
    )
    return decision, row.ts


async def _from_handoff(
    session: AsyncSession, ticket_id: uuid.UUID, started_at: datetime
) -> tuple[ReviewDecision, datetime] | None:
    row = (
        await session.execute(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "ticket",
                AuditLog.entity_id == str(ticket_id),
                AuditLog.action.in_(_HANDOFF_DECISIONS),
                AuditLog.actor_type == ActorType.OPERATOR,
                AuditLog.ts >= started_at,
            )
            .order_by(AuditLog.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None or row.actor_id is None:
        logger.warning("lost resume not rebuilt", extra={"ticket_id": str(ticket_id)})
        return None
    if row.action == "ticket.closed":
        return ReviewDecision(kind="close", operator_id=row.actor_id), row.ts
    try:
        message_id = uuid.UUID(str(row.details.get("message_id")))
    except ValueError:
        # One bad audit row must not stop the recovery of every other run.
        logger.warning("lost resume not rebuilt", extra={"ticket_id": str(ticket_id)})
        return None
    message = await session.get(Message, message_id)
    if message is None:  # erased meanwhile
        return None
    decision = ReviewDecision(
        kind="reply", operator_id=row.actor_id, body=message.body, message_id=str(message.id)
    )
    return decision, row.ts


async def _first_audit(
    session: AsyncSession, action: str, *, entity_type: str, entity_id: uuid.UUID
) -> AuditLog | None:
    statement = (
        select(AuditLog)
        .where(
            AuditLog.action == action,
            AuditLog.entity_type == entity_type,
            AuditLog.entity_id == str(entity_id),
        )
        .order_by(AuditLog.id)
        .limit(1)
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def resume_lost(pipeline: Pipeline, lost: LostResume, recovery: Recovery) -> bool | None:
    """Resume one lost resume with its rebuilt decision. True when the run ended, False when it
    failed (the ticket is already with a human), None when this process works on it."""
    if lost.run_id in pipeline.live:
        return None
    try:
        if not await paused_at(pipeline, lost.ticket_id, lost.run_id):
            # The checkpoint no longer holds the pause (e.g. erased): nothing to continue.
            raise RunIncompleteError
        async with pipeline.database.sessions.begin() as session:
            await record(
                session,
                actor_type=recovery.actor_type,
                actor_id=recovery.actor_id,
                action="run.recovered",
                entity_type="run",
                entity_id=lost.run_id,
                details={
                    "ticket_id": lost.ticket_id,
                    "from": "lost_resume",
                    "kind": lost.decision.kind,
                },
            )
    except Exception as exc:
        await settle_failure(pipeline, lost.ticket_id, lost.run_id, exc)
        return False
    logger.info("lost resume recovered", extra={"run_id": str(lost.run_id)})
    return await resume_run(
        pipeline, ticket_id=lost.ticket_id, run_id=lost.run_id, decision=lost.decision
    )


async def recover_run(pipeline: Pipeline, stuck: StuckRun, recovery: Recovery) -> bool | None:
    """Continue one stuck run. True when it ended without a failure, False when it failed (and
    was handed to a human), None when this process already works on it."""
    if not pipeline.live.claim(stuck.run_id):
        return None
    try:
        graph = await pipeline.checkpoints.graph(compile_graph)
        snapshot = await graph.aget_state(thread(stuck.ticket_id))
        values: dict[str, Any] = snapshot.values
        start: TicketState | None
        if values.get("run_id") == str(stuck.run_id):
            if not snapshot.next:
                # The graph ended but the run row was not finished: nothing to continue.
                raise RunIncompleteError
            start, how = None, "checkpoint"  # None: continue from the last checkpoint
        else:
            if stuck.message_id is None:  # the trigger message was erased
                raise RunIncompleteError
            start, how = (
                run_input(
                    ticket_id=str(stuck.ticket_id),
                    run_id=str(stuck.run_id),
                    message_id=str(stuck.message_id),
                    customer_ref=str(stuck.customer_id),
                ),
                "restart",
            )
        async with pipeline.database.sessions.begin() as session:
            await record(
                session,
                actor_type=recovery.actor_type,
                actor_id=recovery.actor_id,
                action="run.recovered",
                entity_type="run",
                entity_id=stuck.run_id,
                details={"ticket_id": stuck.ticket_id, "from": how},
            )
        logger.info("run recovered", extra={"run_id": str(stuck.run_id), "from": how})
        final = await invoke(
            pipeline,
            graph,
            start,
            ticket_id=stuck.ticket_id,
            run_id=stuck.run_id,
            context=GraphContext(pipeline=pipeline, emit=ignore),
            how="recover",
        )
        await outcome_status(pipeline, stuck.ticket_id, final)
        return not final.get("errors")
    except Exception as exc:
        await settle_failure(pipeline, stuck.ticket_id, stuck.run_id, exc)
        return False
    finally:
        pipeline.live.release(stuck.run_id)


async def recover_ticket_later(pipeline: Pipeline, ticket_id: uuid.UUID) -> None:
    """The lazy path: recover the ticket's stuck runs. Run as a task of its own; never raises."""
    try:
        await recover_runs(pipeline, Recovery(), ticket_id=ticket_id)
    except Exception as exc:
        logger.warning("lazy recovery failed", extra={"error": type(exc).__name__})
