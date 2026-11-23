"""Operator console endpoints (docs/api.md §3): queue, ticket, claim, release, reply, close (phase
2). The draft review (approve, edit, reject; P3-05b) is in `console_drafts`, the actions an
operator runs by hand (P7-05) in `console_actions`.

Every route declares its role with `RequireRole` (a test walks all `/console` and `/admin`
routes); state-changing routes also need the CSRF token, which `RequireRole` checks. Every
action writes an audit record in the same transaction as the change.

A ticket in `processing` reaches the operators through the queue while its run works (or when
no run started): claiming it hands it off first (`processing → handed_off`, reason
`operator_takeover`, ai-pipeline §6), and then the operator owns it like any handoff. There is
no direct operator reply on a `processing` ticket: that would let an operator and the AI answer
the same ticket.

Paused runs (P6-03, ai-pipeline §1): the first reply on a `handed_off` ticket resumes the
ticket's paused run (`kind = reply`), a close resumes it with `kind = close`; both after the
commit (`hub.api.resume`). Later replies are plain messages. Reading a ticket in `processing`
starts the recovery of a stuck run.
"""

import base64
import binascii
import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hub.api.console_schemas import (
    AssigneeOut,
    ConsoleCustomerOut,
    ConsoleMessageOut,
    ConsoleTicketOut,
    DraftOut,
    MemoryOut,
    ModelCallOut,
    QueueTicketOut,
    ReplyIn,
    RunOut,
    TicketVersionIn,
    ToolCallOut,
)
from hub.api.dependencies import ContainerDep, RequireRole, SessionDep
from hub.api.errors import ProblemError
from hub.api.message_helpers import audit_masking, author_label, masked_entities
from hub.api.resume import (
    latest_paused_run,
    recover_on_read,
    resume_after_commit,
    write_memory,
)
from hub.api.schemas import Page
from hub.audit import record
from hub.auth.roles import OPERATOR, TICKET_READER, can_override_claim
from hub.db.models import Message, Operator, Ticket
from hub.db.repositories import (
    ConcurrentUpdateError,
    DraftRepository,
    LlmCallRepository,
    MemoryRepository,
    MessageRepository,
    QueueCursor,
    QueueFilters,
    QueueRepository,
    QueueRow,
    RunRepository,
    TicketRepository,
    ToolCallRepository,
)
from hub.domain.enums import (
    ActorType,
    Intent,
    MessageRole,
    Resolution,
    Route,
    RouteReason,
    TicketStatus,
    Urgency,
)
from hub.domain.memory import MEMORY_ITEMS
from hub.domain.review import ReviewDecision
from hub.guardrails.masking import mask

router = APIRouter(prefix="/api/v1/console", tags=["console"])

Reader = Annotated[Operator, Depends(RequireRole(TICKET_READER))]
Writer = Annotated[Operator, Depends(RequireRole(OPERATOR))]
IdempotencyKey = Annotated[uuid.UUID, Header(alias="Idempotency-Key")]

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
OPERATOR_TAKEOVER = RouteReason.OPERATOR_TAKEOVER.value
# An operator may answer a ticket that waits for a human or was answered already. A draft that
# waits (`pending_approval`) is answered with the draft endpoints (`console_drafts`).
_REPLYABLE = frozenset({TicketStatus.HANDED_OFF, TicketStatus.ANSWERED})
_CLOSABLE = frozenset({TicketStatus.HANDED_OFF, TicketStatus.ANSWERED})


# --- views ----------------------------------------------------------------------------------


def _row_out(row: QueueRow) -> QueueTicketOut:
    ticket, customer, assignee = row.ticket, row.customer, row.assignee
    return QueueTicketOut(
        id=ticket.id,
        status=ticket.status,
        intent=ticket.intent,
        urgency=ticket.urgency,
        summary=ticket.summary,
        last_route=ticket.last_route,
        route_reasons=[str(reason) for reason in ticket.route_reasons],
        customer=ConsoleCustomerOut(
            id=customer.id,
            customer_ref=customer.external_ref,
            display_name=customer.display_name,
            tier=customer.tier,
            country=customer.country,
        ),
        assigned_operator=None
        if assignee is None
        else AssigneeOut(id=assignee.id, name=assignee.name),
        waiting_since=row.waiting_since,
        created_at=ticket.created_at,
        version=ticket.version,
    )


async def _message_out(session: AsyncSession, message: Message) -> ConsoleMessageOut:
    return ConsoleMessageOut(
        id=message.id,
        role=message.role,
        author_label=await author_label(session, message),
        is_ai_generated=message.is_ai_generated,
        author_operator_id=message.author_operator_id,
        body=message.body,
        citations=list(message.citations),
        masked_entities=dict(message.masked_entities),
        created_at=message.created_at,
    )


def _encode_cursor(cursor: QueueCursor) -> str:
    raw = json.dumps([cursor.urgency_rank, cursor.waiting_since.isoformat(), str(cursor.ticket_id)])
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(value: str) -> QueueCursor:
    try:
        rank, waiting, ticket_id = json.loads(base64.urlsafe_b64decode(value.encode()))
        cursor = QueueCursor(int(rank), datetime.fromisoformat(waiting), uuid.UUID(ticket_id))
    except (ValueError, TypeError, binascii.Error):
        raise ProblemError(422, "validation_error", "The cursor is not valid.") from None
    if cursor.waiting_since.tzinfo is None:
        raise ProblemError(422, "validation_error", "The cursor is not valid.")
    return cursor


def _assigned_filter(
    assigned: Literal["me", "none", "any"], operator: Operator
) -> uuid.UUID | Literal["none"] | None:
    if assigned == "me":
        return operator.id
    if assigned == "none":
        return "none"
    return None


# --- shared steps ---------------------------------------------------------------------------


async def _ticket_or_404(session: AsyncSession, ticket_id: uuid.UUID) -> Ticket:
    ticket = await TicketRepository(session).get(ticket_id)
    if ticket is None:
        raise ProblemError(404, "not_found", "Ticket not found.")
    return ticket


def _check_version(ticket: Ticket, ticket_version: int) -> None:
    if ticket.version != ticket_version:
        raise ProblemError(
            409, "stale_draft", "The ticket changed since you loaded it. Reload and try again."
        )


def _check_not_closed(ticket: Ticket) -> None:
    if ticket.status == TicketStatus.CLOSED:
        raise ProblemError(409, "ticket_closed", "This ticket is closed.")


def _check_not_claimed_by_other(ticket: Ticket, operator: Operator) -> None:
    """Another operator's claim blocks the change; a lead or admin may act on it anyway."""
    owner = ticket.assigned_operator_id
    if owner is not None and owner != operator.id and not can_override_claim(operator.role):
        raise ProblemError(409, "conflict", "Another operator has claimed this ticket.")


async def _save(session: AsyncSession, ticket: Ticket) -> None:
    try:
        await TicketRepository(session).save(ticket)
    except ConcurrentUpdateError:
        raise ProblemError(
            409, "stale_draft", "The ticket changed since you loaded it. Reload and try again."
        ) from None


async def _change_status(session: AsyncSession, ticket: Ticket, target: TicketStatus) -> None:
    try:
        await TicketRepository(session).change_status(ticket, target)
    except ConcurrentUpdateError:
        raise ProblemError(
            409, "stale_draft", "The ticket changed since you loaded it. Reload and try again."
        ) from None


async def _audit(
    session: AsyncSession,
    operator: Operator,
    action: str,
    ticket_id: uuid.UUID,
    details: dict[str, object] | None = None,
) -> None:
    await record(
        session,
        actor_type=ActorType.OPERATOR,
        actor_id=operator.id,
        action=action,
        entity_type="ticket",
        entity_id=ticket_id,
        details=details,
    )


async def _row(session: AsyncSession, ticket_id: uuid.UUID) -> QueueTicketOut:
    row = await QueueRepository(session).get(ticket_id)
    if row is None:  # pragma: no cover - the ticket was just read in this request
        raise ProblemError(404, "not_found", "Ticket not found.")
    return _row_out(row)


# --- endpoints ------------------------------------------------------------------------------


@router.get("/tickets")
async def list_tickets(
    operator: Reader,
    session: SessionDep,
    status: Annotated[list[TicketStatus] | None, Query()] = None,
    intent: Intent | None = None,
    urgency: Urgency | None = None,
    assigned: Literal["me", "none", "any"] = "any",
    route_reason: Annotated[str | None, Query(pattern=r"^[a-z_]{1,40}$")] = None,
    q: Annotated[str | None, Query(min_length=1, max_length=200, pattern=r"^[^\x00]*$")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(max_length=500)] = None,
) -> Page[QueueTicketOut]:
    """The queue. Order: urgency desc, waiting time desc (api.md §3). Without `status` every
    status is listed, `processing` included (tickets without a run in phase 2)."""
    filters = QueueFilters(
        statuses=frozenset(status) if status else None,
        intent=intent,
        urgency=urgency,
        assigned_to=_assigned_filter(assigned, operator),
        route_reason=route_reason,
        q=q,
    )
    after = _decode_cursor(cursor) if cursor is not None else None
    rows = await QueueRepository(session).page(filters, limit=limit + 1, after=after)
    page, more = rows[:limit], len(rows) > limit
    return Page[QueueTicketOut](
        items=[_row_out(row) for row in page],
        next_cursor=_encode_cursor(page[-1].cursor) if more else None,
    )


@router.get("/tickets/{ticket_id}")
async def get_ticket(
    ticket_id: uuid.UUID, _operator: Reader, session: SessionDep, container: ContainerDep
) -> ConsoleTicketOut:
    row = await QueueRepository(session).get(ticket_id)
    if row is None:
        raise ProblemError(404, "not_found", "Ticket not found.")
    ticket = row.ticket
    await recover_on_read(container, session, ticket)
    messages = await MessageRepository(session).list_for_ticket(ticket.id)
    run = await RunRepository(session).latest_for_ticket(ticket.id)
    draft = None if run is None else await DraftRepository(session).for_run(run.id)
    tool_calls = await ToolCallRepository(session).for_ticket(ticket.id)
    model_calls = [] if run is None else await LlmCallRepository(session).for_run(run.id)
    memory = await MemoryRepository(session).active_for_customer(
        ticket.customer_id, limit=MEMORY_ITEMS, exclude_ticket=ticket.id
    )
    return ConsoleTicketOut(
        **_row_out(row).model_dump(),
        language=ticket.language,
        confidence=ticket.confidence,
        ai_disclosed_at=ticket.ai_disclosed_at,
        first_response_at=ticket.first_response_at,
        resolved_at=ticket.resolved_at,
        resolution=None if ticket.resolution is None else ticket.resolution.value,
        messages=[await _message_out(session, message) for message in messages],
        latest_run=None if run is None else RunOut.model_validate(run),
        draft=None if draft is None else DraftOut.model_validate(draft),
        memory=[
            MemoryOut(
                ticket_id=item.ticket_id,
                intent=intent,
                summary=item.content,
                created_at=item.created_at,
                expires_at=item.expires_at,
            )
            for item, intent in memory
        ],
        tool_calls=[ToolCallOut.model_validate(call) for call in tool_calls],
        model_calls=[ModelCallOut.model_validate(call) for call in model_calls],
    )


@router.post("/tickets/{ticket_id}/claim")
async def claim_ticket(
    ticket_id: uuid.UUID,
    body: TicketVersionIn,
    operator: Writer,
    session: SessionDep,
    force: bool = False,
) -> QueueTicketOut:
    """Assign the ticket to me. Another operator's claim answers 409; a lead or admin may take
    it over with `?force=true`. A `processing` ticket is handed off first (module docstring)."""
    ticket = await _ticket_or_404(session, ticket_id)
    _check_version(ticket, body.ticket_version)
    _check_not_closed(ticket)
    previous = ticket.assigned_operator_id
    if previous == operator.id:
        return await _row(session, ticket.id)
    if previous is not None:
        if not force:
            raise ProblemError(409, "conflict", "Another operator has claimed this ticket.")
        if not can_override_claim(operator.role):
            raise ProblemError(403, "forbidden", "Only a lead can take over a claimed ticket.")

    if ticket.status == TicketStatus.PROCESSING:
        ticket.last_route = Route.HANDOFF
        ticket.route_reasons = [OPERATOR_TAKEOVER]
        await _change_status(session, ticket, TicketStatus.HANDED_OFF)
        await _audit(
            session,
            operator,
            "route.decided",
            ticket.id,
            {"route": Route.HANDOFF.value, "reasons": [OPERATOR_TAKEOVER]},
        )
    ticket.assigned_operator_id = operator.id
    await _save(session, ticket)
    await _audit(
        session,
        operator,
        "ticket.claimed",
        ticket.id,
        {"previous_operator_id": previous, "forced": previous is not None},
    )
    await session.commit()
    return await _row(session, ticket.id)


@router.post("/tickets/{ticket_id}/release")
async def release_ticket(
    ticket_id: uuid.UUID, body: TicketVersionIn, operator: Writer, session: SessionDep
) -> QueueTicketOut:
    """Unassign. Only the operator who claimed it, or a lead or admin."""
    ticket = await _ticket_or_404(session, ticket_id)
    _check_version(ticket, body.ticket_version)
    previous = ticket.assigned_operator_id
    if previous is None:
        return await _row(session, ticket.id)
    _check_not_claimed_by_other(ticket, operator)
    ticket.assigned_operator_id = None
    await _save(session, ticket)
    await _audit(
        session, operator, "ticket.released", ticket.id, {"previous_operator_id": previous}
    )
    await session.commit()
    return await _row(session, ticket.id)


@router.post("/tickets/{ticket_id}/reply")
async def reply(
    ticket_id: uuid.UUID,
    body: ReplyIn,
    idempotency_key: IdempotencyKey,
    operator: Writer,
    session: SessionDep,
    container: ContainerDep,
) -> ConsoleMessageOut:
    """Free reply on a handed-off or answered ticket. The text is masked (G1) like a customer
    message; the customer reads it through `GET /tickets/{id}/messages`. The first reply on a
    handed-off ticket resumes its paused run (module docstring); actions come in phase 7."""
    ticket = await _ticket_or_404(session, ticket_id)
    masked = mask(body.body)
    # Scoped to the operator: another operator's key never returns this message.
    scoped_key = f"operator:{operator.id}:{idempotency_key}"
    messages = MessageRepository(session)

    stored = await messages.get_by_idempotency_key(scoped_key)
    if stored is not None:
        return await _replay(session, stored, ticket_id=ticket.id, masked_body=masked.text)

    # Checked under the ticket row lock: a close that committed while this request waited is
    # seen here, so the reply cannot land in a closed ticket.
    await TicketRepository(session).lock(ticket)
    _check_not_closed(ticket)
    _check_version(ticket, body.ticket_version)
    if ticket.status not in _REPLYABLE:
        detail = (
            "Claim the ticket first."
            if ticket.status == TicketStatus.PROCESSING
            else "This ticket has a draft that waits for review."
        )
        raise ProblemError(409, "conflict", detail)
    _check_not_claimed_by_other(ticket, operator)

    ticket_key = ticket.id
    run_id = None
    try:
        if ticket.status == TicketStatus.HANDED_OFF:
            run_id = await latest_paused_run(session, ticket.id)
        message = await messages.add(
            ticket_id=ticket.id,
            role=MessageRole.OPERATOR,
            body=masked.text,
            author_operator_id=operator.id,
            masked_entities=masked_entities(masked),
            idempotency_key=scoped_key,
        )
        if ticket.first_response_at is None:
            ticket.first_response_at = datetime.now(UTC)
        if ticket.status == TicketStatus.HANDED_OFF:
            ticket.resolution = Resolution.HUMAN
            await _change_status(session, ticket, TicketStatus.ANSWERED)
        else:
            # An answered ticket keeps its status; only `first_response_at` may change.
            await _save(session, ticket)
        await _audit(session, operator, "message.sent", ticket.id, {"message_id": message.id})
        await audit_masking(session, masked, entity_type="message", entity_id=message.id)
        decision = ReviewDecision(
            kind="reply",
            operator_id=str(operator.id),
            body=masked.text,
            message_id=str(message.id),
        )
        await session.commit()
    except IntegrityError:
        # A concurrent request with the same key saved first.
        await session.rollback()
        stored = await messages.get_by_idempotency_key(scoped_key)
        if stored is None:
            raise
        return await _replay(session, stored, ticket_id=ticket_key, masked_body=masked.text)
    await resume_after_commit(container, ticket_key, run_id, decision)
    return await _message_out(session, message)


async def _replay(
    session: AsyncSession, stored: Message, *, ticket_id: uuid.UUID, masked_body: str
) -> ConsoleMessageOut:
    """The first answer for a repeated `Idempotency-Key`, when it was the same request."""
    if stored.ticket_id != ticket_id or stored.body != masked_body:
        raise ProblemError(
            409,
            "idempotency_key_reused",
            "This Idempotency-Key was already used for a different request.",
        )
    return await _message_out(session, stored)


@router.post("/tickets/{ticket_id}/close")
async def close_ticket(
    ticket_id: uuid.UUID,
    body: TicketVersionIn,
    operator: Writer,
    session: SessionDep,
    container: ContainerDep,
) -> QueueTicketOut:
    """Close an answered or handed-off ticket (state diagram). A run still paused (a handoff
    closed without an answer) is resumed with `kind = close` and ends without a message. Then
    the memory summary is written (P6-05, ai-pipeline §9), inside the request like the resume
    (Cloud Run gives CPU only during a request); it never fails the close."""
    ticket = await _ticket_or_404(session, ticket_id)
    _check_version(ticket, body.ticket_version)
    _check_not_closed(ticket)
    if ticket.status not in _CLOSABLE:
        raise ProblemError(409, "conflict", f"A ticket in {ticket.status} cannot be closed.")
    _check_not_claimed_by_other(ticket, operator)
    previous = ticket.status
    run_id = await latest_paused_run(session, ticket.id)
    if ticket.resolved_at is None:
        ticket.resolved_at = datetime.now(UTC)
    await _change_status(session, ticket, TicketStatus.CLOSED)
    await _audit(session, operator, "ticket.closed", ticket.id, {"from_status": previous.value})
    await session.commit()
    decision = ReviewDecision(kind="close", operator_id=str(operator.id))
    await resume_after_commit(container, ticket_id, run_id, decision)
    await write_memory(container, ticket_id)
    return await _row(session, ticket_id)
