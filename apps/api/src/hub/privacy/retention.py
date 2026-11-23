"""The retention job (security §6, NFR-13, P8-05): `POST /admin/jobs/retention`.

1. Tickets without activity (their newest message, or their creation) for
   `RETENTION_TICKET_DAYS` (24 months) are deleted with their messages, drafts, runs, tool
   calls, feedback and memory. `llm_calls` stay (`ON DELETE SET NULL`): they are the cost record.
2. Expired memory (`customer_memory.expires_at`, 12 months) is deleted.
3. The checkpoints of tickets closed more than `RETENTION_CHECKPOINT_DAYS` (7) ago, and of the
   tickets deleted in 1, are returned for the API to delete through the checkpoint store.

Not here: traces (Langfuse keeps them 30 days and deletes them itself, P9-01) and the audit log
(5 years): the app role cannot delete audit rows (security §4), and removing the oldest rows
would need the chain to be anchored anew, which is outside this job.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from hub.db.models import AuditLog, CustomerMemory, Message, Run, Ticket, ToolCall
from hub.domain.enums import TicketStatus
from hub.privacy._content import delete_content, rowcount, run_ids


@dataclass(frozen=True)
class RetentionReport:
    tickets_deleted: int
    messages_deleted: int
    drafts_deleted: int
    runs_deleted: int
    tool_calls_deleted: int
    feedback_deleted: int
    memory_deleted: int
    # Not in the API answer: the checkpoint threads the API deletes after the commit.
    checkpoint_ticket_ids: tuple[uuid.UUID, ...] = field(default=())


async def run_retention(
    session: AsyncSession, *, ticket_days: int, checkpoint_days: int
) -> RetentionReport:
    """Delete inside the caller's transaction. Times are the database's (`now()`)."""
    now = (await session.execute(select(func.now()))).scalar_one()
    old = await _inactive_tickets(session, now - timedelta(days=ticket_days))
    runs = await run_ids(session, [ticket.id for ticket in old])
    tool_calls = (
        await rowcount(session, delete(ToolCall).where(ToolCall.run_id.in_(runs))) if runs else 0
    )
    deleted = await delete_content(session, [ticket.id for ticket in old])
    runs_deleted = await rowcount(session, delete(Run).where(Run.id.in_(runs))) if runs else 0
    for ticket in old:
        await session.delete(ticket)  # one by one: `tickets` takes no bulk statement
    expired = await rowcount(session, delete(CustomerMemory).where(CustomerMemory.expires_at < now))
    await session.flush()
    closed = await _closed_with_checkpoints(session, now - timedelta(days=checkpoint_days))
    return RetentionReport(
        tickets_deleted=len(old),
        messages_deleted=deleted.messages,
        drafts_deleted=deleted.drafts,
        runs_deleted=runs_deleted,
        tool_calls_deleted=tool_calls,
        feedback_deleted=deleted.feedback,
        memory_deleted=deleted.memory + expired,
        checkpoint_ticket_ids=tuple(dict.fromkeys([*(t.id for t in old), *closed])),
    )


async def _inactive_tickets(session: AsyncSession, cutoff: datetime) -> Sequence[Ticket]:
    newest = (
        select(Message.ticket_id, func.max(Message.created_at).label("newest"))
        .group_by(Message.ticket_id)
        .subquery()
    )
    statement = (
        select(Ticket)
        .outerjoin(newest, newest.c.ticket_id == Ticket.id)
        .where(func.coalesce(newest.c.newest, Ticket.created_at) < cutoff)
        .order_by(Ticket.id)
    )
    return list((await session.execute(statement)).scalars())


async def _closed_with_checkpoints(session: AsyncSession, cutoff: datetime) -> list[uuid.UUID]:
    """Closed tickets whose close (`ticket.closed`, the newest one) is older than `cutoff` and
    that still have checkpoints. The checkpoint table is only read here, by its thread id."""
    found = await session.execute(text("SELECT DISTINCT thread_id FROM checkpoints"))
    threads = set(found.scalars())
    if not threads:
        return []
    closed = (
        await session.execute(select(Ticket.id).where(Ticket.status == TicketStatus.CLOSED))
    ).scalars()
    candidates = [str(ticket_id) for ticket_id in closed if str(ticket_id) in threads]
    if not candidates:
        return []
    rows = await session.execute(
        select(AuditLog.entity_id)
        .where(
            AuditLog.action == "ticket.closed",
            AuditLog.entity_type == "ticket",
            AuditLog.entity_id.in_(candidates),
        )
        .group_by(AuditLog.entity_id)
        .having(func.max(AuditLog.ts) < cutoff)
    )
    return sorted(uuid.UUID(entity_id) for entity_id in rows.scalars())
