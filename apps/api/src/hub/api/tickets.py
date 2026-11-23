"""Customer ticket endpoints (docs/api.md §2).

Order inside a message handler (ADR-0010, security G1): validate → mask → save → audit, in one
transaction that is committed before the first byte of the SSE stream. Validation is the
request schema (4,000 characters), which runs before the handler, so an over-long message is
refused before the mask sees it; the body cap in `hub/api/limits.py` runs before that. The raw
text lives only in this request's memory: it is never logged, stored or put in an error.

A new ticket starts a run of the workflow graph (`hub.graph.run`, P6-01) after the commit. The
run is a task of its own, so a closed stream does not stop it; the stream relays its events:
`ticket.created` → `run.status` (understanding, searching, writing, checking) → `message` →
`ticket.status` → `done` (api.md §4). The `message` is the one the run saved (the handoff notice
or an auto-reply: the run sends its id, the stream sends the stored row, P6-02), or, when the run
saved none (approval, or a run that failed before a notice), a placeholder system message;
`error` comes before `ticket.status` when a node failed. A `: ping` comment goes out every 10 s
while the run works (sent by `_run_stream` itself: FastAPI's own 15 s keep-alive applies only to
generator endpoints, which these are not).

Ownership rule for a follow-up message (data model §1, P6-03): on an `answered` ticket the AI
still owns (no handoff or approval so far) it starts a new run like a new ticket; once a human
owns the ticket it goes to the operator queue (`handed_off`). A message on a ticket in any other
status starts no run: the ticket is in a run already (`processing`) or with a human. Every
customer message changes the ticket's `version` (`MessageRepository.add`), so an operator's
decision loaded before it answers `409 stale_draft`. A follow-up without a run and a ticket
handed off because masking failed stream `ticket.created` → placeholder → (`ticket.status`) →
`done`; a replay of the same request (`Idempotency-Key`) always carries the ticket's current
status.

A read of a ticket in `processing` (the ticket or its messages) starts the recovery of a stuck
run (`hub.api.resume.recover_on_read`). The placeholder is not stored:
it stands in for an answer and would otherwise leave invented history in every ticket.

Audit order, the same for a new ticket and a follow-up: `ticket.created` or `message.received`,
then `guardrail.masked`, then `route.decided` when a failed mask handed the ticket off.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.sse import EventSourceResponse, format_sse_event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hub.api.dependencies import ContainerDep, CustomerDep, SessionDep, client_ip
from hub.api.errors import ProblemError
from hub.api.message_helpers import SYSTEM_LABEL, audit_masking, author_label, masked_entities
from hub.api.resume import recover_on_read
from hub.api.schemas import (
    FeedbackIn,
    FeedbackOut,
    MessageIn,
    MessageOut,
    Page,
    TicketCreatedEvent,
    TicketOut,
    citations_for_customer,
)
from hub.audit import record
from hub.db.models import Customer, Message, Ticket, new_id
from hub.db.repositories import (
    ConcurrentUpdateError,
    FeedbackRepository,
    MessageRepository,
    TicketRepository,
)
from hub.deps import Container
from hub.domain.enums import ActorType, MessageRole, Route, TicketStatus
from hub.graph.pipeline import Pipeline
from hub.graph.run import run_ticket
from hub.guardrails.masking import CARD_ENTITIES, MaskResult, mask

router = APIRouter(prefix="/api/v1/tickets", tags=["tickets"])

PLACEHOLDER_TEXT = (
    "Thanks, we have your message. Our support team will reply in this chat. "
    "You can close this window; the answer will wait for you here."
)
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100

# The handlers return the stream themselves, after the commit. `response_class=EventSourceResponse`
# is not used: FastAPI 0.162 then expects the endpoint to be a generator and iterates its result,
# and a generator endpoint would run the masking and saving only once streaming has started,
# too late to answer 404 or 409. The OpenAPI document still names the media type.
SSE_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "Server-Sent Events (docs/api.md §4)",
        "content": {"text/event-stream": {}},
    }
}

# api.md §4: a comment line every 10 s keeps the connection open while the run works.
PING_INTERVAL_S = 10.0

IdempotencyKey = Annotated[uuid.UUID, Header(alias="Idempotency-Key")]


# --- views -----------------------------------------------------------------------------------


def _ticket_out(ticket: Ticket) -> TicketOut:
    return TicketOut(
        id=ticket.id,
        status=ticket.status,
        ai_disclosed_at=ticket.ai_disclosed_at,
        created_at=ticket.created_at,
    )


async def _message_out(session: AsyncSession, message: Message) -> MessageOut:
    return MessageOut(
        id=message.id,
        role=message.role,
        author_label=await author_label(session, message),
        is_ai_generated=message.is_ai_generated,
        body=message.body,
        citations=citations_for_customer(message.citations),
        # Card data only: the note says "We hid a card number" (docs/ui.md); an IBAN is not.
        masked=any(
            count > 0
            for kind, count in message.masked_entities.items()
            if kind in CARD_ENTITIES or kind == "failed"
        ),
        created_at=message.created_at,
    )


def _placeholder() -> MessageOut:
    return MessageOut(
        id=new_id(),
        role=MessageRole.SYSTEM,
        author_label=SYSTEM_LABEL,
        is_ai_generated=False,
        body=PLACEHOLDER_TEXT,
        citations=[],
        masked=False,
        created_at=datetime.now(UTC),
    )


# --- SSE ------------------------------------------------------------------------------------


def _event(name: str, data: Any) -> bytes:  # noqa: ANN401 - any JSON-ready value
    return format_sse_event(event=name, data_str=json.dumps(data, separators=(",", ":")))


def _stream(
    ticket_id: uuid.UUID, message: MessageOut, status: TicketStatus | None = None
) -> EventSourceResponse:
    """`status` is sent as `ticket.status`: when this request handed the ticket to a human, and
    on a replay."""
    created = TicketCreatedEvent(ticket_id=ticket_id, message=message).model_dump(mode="json")
    placeholder = _placeholder().model_dump(mode="json")

    async def events() -> AsyncIterator[bytes]:
        yield _event("ticket.created", created)
        yield _event("message", placeholder)
        if status is not None:
            yield _event("ticket.status", {"status": status.value})
        yield _event("done", {})

    return _sse(events())


async def _stored_message(
    container: Container, ticket_id: uuid.UUID, data: dict[str, Any]
) -> MessageOut | None:
    """The customer view of a message the run saved, by the id in the run's event; None when
    it is not a message of this ticket."""
    if container.database is None:  # pragma: no cover - a run needs the database
        return None
    async with container.database.sessions() as session:
        message = await MessageRepository(session).get_in_ticket(
            ticket_id, uuid.UUID(str(data["message_id"]))
        )
        return None if message is None else await _message_out(session, message)


def _sse(events: AsyncIterator[bytes]) -> EventSourceResponse:
    # No buffering in proxies, no caching: the events must reach the browser as they are sent.
    return EventSourceResponse(
        events, headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


def _run_stream(
    container: Container, pipeline: Pipeline, message: MessageOut, ticket_id: uuid.UUID
) -> EventSourceResponse:
    """Start the run and relay its events (module docstring). The queue ends with `None`."""
    created = TicketCreatedEvent(ticket_id=ticket_id, message=message).model_dump(mode="json")
    queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()

    async def emit(name: str, data: dict[str, Any]) -> None:
        queue.put_nowait((name, data))

    async def job() -> None:
        try:
            await run_ticket(pipeline, ticket_id=ticket_id, message_id=message.id, emit=emit)
        finally:
            queue.put_nowait(None)

    container.runs.start(job())

    async def events() -> AsyncIterator[bytes]:
        yield _event("ticket.created", created)
        status_sent = message_sent = False
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=PING_INTERVAL_S)
            except TimeoutError:
                yield b": ping\n\n"
                continue
            if item is None:
                break
            name, data = item
            if name == "message":
                # A message the run saved (the handoff notice or an auto-reply): the run sends
                # its id, the stream sends the customer view of the stored row.
                stored = await _stored_message(container, ticket_id, data)
                if stored is None:
                    continue
                message_sent = True
                data = stored.model_dump(mode="json")
            if name == "ticket.status":
                status_sent = True
                if not message_sent:
                    yield _event("message", _placeholder().model_dump(mode="json"))
            yield _event(name, data)
        if not status_sent:
            # A run that ended without a status (it crashed in a way `run_ticket` does not
            # catch): the stream still follows api.md §4; the ticket waits in the queue.
            yield _event("message", _placeholder().model_dump(mode="json"))
            yield _event("ticket.status", {"status": TicketStatus.PROCESSING.value})
        yield _event("done", {})

    return _sse(events())


# --- shared steps ---------------------------------------------------------------------------


def _check_rate_limits(container: Container, customer: Customer, request: Request) -> None:
    limits = container.rate_limits
    session_key, ip_key = str(customer.id), client_ip(request)
    retry_after = max(
        limits.messages_per_session.retry_after(session_key) or 0,
        limits.messages_per_ip.retry_after(ip_key) or 0,
    )
    if retry_after:
        raise ProblemError(
            429,
            "too_many_requests",
            "Too many messages. Please wait a moment.",
            headers={"Retry-After": str(retry_after)},
        )
    limits.messages_per_session.hit(session_key)
    limits.messages_per_ip.hit(ip_key)


def _scoped_key(customer: Customer, key: uuid.UUID) -> str:
    # Scoped to the customer: another customer sending the same key gets their own message and
    # can never receive this one.
    return f"{customer.id}:{key}"


async def _ticket_or_404(session: AsyncSession, ticket_id: uuid.UUID, customer: Customer) -> Ticket:
    # 404, not 403, for another customer's ticket: its existence is not revealed (api.md §2).
    ticket = await TicketRepository(session).get_for_customer(ticket_id, customer.id)
    if ticket is None:
        raise ProblemError(404, "not_found", "Ticket not found.")
    return ticket


async def _replay(
    session: AsyncSession,
    stored: Message,
    *,
    expected_ticket_id: uuid.UUID | None,
    masked_body: str,
) -> EventSourceResponse:
    """The stream for a repeated `Idempotency-Key`: the first message, not a new one, and the
    ticket's current status (the first response may have carried one, e.g. after a failed
    mask, and a replay must not look different).

    The key must have been used for the same request: same ticket (for a follow-up message),
    the first message of its ticket (for a new ticket), and the same text after masking.
    """
    same_request = stored.role == MessageRole.CUSTOMER and stored.body == masked_body
    if expected_ticket_id is not None:
        same_request = same_request and stored.ticket_id == expected_ticket_id
    else:
        first = (await MessageRepository(session).list_for_ticket(stored.ticket_id))[0]
        same_request = same_request and first.id == stored.id
    if not same_request:
        raise ProblemError(
            409,
            "idempotency_key_reused",
            "This Idempotency-Key was already used for a different request.",
        )
    current = await session.get(Ticket, stored.ticket_id)
    status = current.status if current is not None else None
    return _stream(stored.ticket_id, await _message_out(session, stored), status)


async def _audit_message(
    session: AsyncSession,
    *,
    customer_id: uuid.UUID,
    ticket_id: uuid.UUID,
    message_id: uuid.UUID,
    masked: MaskResult,
    action: str,
) -> None:
    # Ids and counts only: no message text in the audit log (security §5).
    await record(
        session,
        actor_type=ActorType.CUSTOMER,
        actor_id=customer_id,
        action=action,
        entity_type="ticket",
        entity_id=ticket_id,
        details={"message_id": message_id},
    )
    await audit_masking(session, masked, entity_type="message", entity_id=message_id)


async def _change_status_or_409(
    session: AsyncSession, ticket: Ticket, target: TicketStatus
) -> None:
    try:
        await TicketRepository(session).change_status(ticket, target)
    except ConcurrentUpdateError:
        raise ProblemError(409, "conflict", "The ticket changed. Please try again.") from None


async def _hand_off_after_mask_failure(
    session: AsyncSession, ticket: Ticket, masked: MaskResult
) -> TicketStatus | None:
    """On any error the ticket goes to a human (AGENTS.md): a failed mask hands it off with
    reason `system_error`. A ticket that already waits for a human keeps its status."""
    if not masked.failed or ticket.status not in (TicketStatus.PROCESSING, TicketStatus.ANSWERED):
        return None
    ticket.last_route = Route.HANDOFF
    ticket.route_reasons = ["system_error"]
    await _change_status_or_409(session, ticket, TicketStatus.HANDED_OFF)
    await record(
        session,
        actor_type=ActorType.SYSTEM,
        actor_id=None,
        action="route.decided",
        entity_type="ticket",
        entity_id=ticket.id,
        details={"route": Route.HANDOFF.value, "reasons": ["system_error"]},
    )
    return TicketStatus.HANDED_OFF


async def _save_or_replay(
    session: AsyncSession,
    save: Callable[[], Awaitable[tuple[Message, TicketStatus | None, bool]]],
    scoped_key: str,
    *,
    ticket_id: uuid.UUID | None,
    masked_body: str,
    run_in: Container | None = None,
) -> EventSourceResponse:
    """Run `save` (writes and commit), then stream its message. When a concurrent request with
    the same key saved first, the unique constraint on `messages.idempotency_key` fails (at the
    flush of the message or at commit); answer with that request's message instead.

    `save` returns the message, the status it handed the ticket off with (or None) and whether
    the message starts a run. `run_in`: the container whose pipeline runs it. A replay never
    starts a run.

    `ticket_id` is passed as a value: the rollback expires every loaded object."""
    try:
        message, status, starts_run = await save()
    except IntegrityError:
        await session.rollback()
        stored = await MessageRepository(session).get_by_idempotency_key(scoped_key)
        if stored is None:
            raise
        return await _replay(session, stored, expected_ticket_id=ticket_id, masked_body=masked_body)
    out = await _message_out(session, message)
    if run_in is not None and run_in.pipeline is not None and starts_run:
        return _run_stream(run_in, run_in.pipeline, out, message.ticket_id)
    return _stream(message.ticket_id, out, status)


def _human_owns(ticket: Ticket) -> bool:
    """After the first handoff or approval in a ticket, a human owns it (data model §1)."""
    return ticket.last_route in (Route.APPROVAL, Route.HANDOFF)


# --- endpoints ------------------------------------------------------------------------------


@router.post("", responses=SSE_RESPONSES)
async def create_ticket(
    body: MessageIn,
    request: Request,
    idempotency_key: IdempotencyKey,
    container: ContainerDep,
    customer: CustomerDep,
    session: SessionDep,
) -> EventSourceResponse:
    _check_rate_limits(container, customer, request)
    masked = mask(body.message)
    scoped_key = _scoped_key(customer, idempotency_key)
    messages = MessageRepository(session)

    stored = await messages.get_by_idempotency_key(scoped_key)
    if stored is not None:
        return await _replay(session, stored, expected_ticket_id=None, masked_body=masked.text)

    customer_id = customer.id

    async def save() -> tuple[Message, TicketStatus | None, bool]:
        ticket = await TicketRepository(session).create(
            customer_id=customer_id, ai_disclosed_at=datetime.now(UTC)
        )
        message = await messages.add(
            ticket_id=ticket.id,
            role=MessageRole.CUSTOMER,
            body=masked.text,
            masked_entities=masked_entities(masked),
            idempotency_key=scoped_key,
        )
        await _audit_message(
            session,
            customer_id=customer_id,
            ticket_id=ticket.id,
            message_id=message.id,
            masked=masked,
            action="ticket.created",
        )
        status = await _hand_off_after_mask_failure(session, ticket, masked)
        await session.commit()
        return message, status, status is None

    return await _save_or_replay(
        session, save, scoped_key, ticket_id=None, masked_body=masked.text, run_in=container
    )


@router.post("/{ticket_id}/messages", responses=SSE_RESPONSES)
async def add_message(
    ticket_id: uuid.UUID,
    body: MessageIn,
    request: Request,
    idempotency_key: IdempotencyKey,
    container: ContainerDep,
    customer: CustomerDep,
    session: SessionDep,
) -> EventSourceResponse:
    ticket = await _ticket_or_404(session, ticket_id, customer)
    _check_rate_limits(container, customer, request)
    masked = mask(body.message)
    scoped_key = _scoped_key(customer, idempotency_key)
    messages = MessageRepository(session)

    stored = await messages.get_by_idempotency_key(scoped_key)
    if stored is not None:
        return await _replay(session, stored, expected_ticket_id=ticket.id, masked_body=masked.text)
    if ticket.status == TicketStatus.CLOSED:
        raise ProblemError(409, "ticket_closed", "This ticket is closed. Start a new one.")

    customer_id, ticket_id = customer.id, ticket.id

    async def save() -> tuple[Message, TicketStatus | None, bool]:
        # The check above ran before the lock: an operator may have closed the ticket since.
        await TicketRepository(session).lock(ticket)
        if ticket.status == TicketStatus.CLOSED:
            raise ProblemError(409, "ticket_closed", "This ticket is closed. Start a new one.")
        message = await messages.add(
            ticket_id=ticket_id,
            role=MessageRole.CUSTOMER,
            body=masked.text,
            masked_entities=masked_entities(masked),
            idempotency_key=scoped_key,
        )
        await _audit_message(
            session,
            customer_id=customer_id,
            ticket_id=ticket_id,
            message_id=message.id,
            masked=masked,
            action="message.received",
        )
        status = await _hand_off_after_mask_failure(session, ticket, masked)
        starts_run = False
        if status is None and ticket.status == TicketStatus.ANSWERED:
            # AI owns → a new run; human owns → the operator queue (module docstring).
            starts_run = not _human_owns(ticket)
            target = TicketStatus.PROCESSING if starts_run else TicketStatus.HANDED_OFF
            await _change_status_or_409(session, ticket, target)
        await session.commit()
        return message, status, starts_run

    return await _save_or_replay(
        session, save, scoped_key, ticket_id=ticket_id, masked_body=masked.text, run_in=container
    )


@router.get("")
async def list_tickets(
    customer: CustomerDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: uuid.UUID | None = None,
) -> Page[TicketOut]:
    tickets = await TicketRepository(session).list_for_customer(
        customer.id, limit=limit + 1, before=cursor
    )
    page, more = tickets[:limit], len(tickets) > limit
    return Page[TicketOut](
        items=[_ticket_out(ticket) for ticket in page],
        next_cursor=str(page[-1].id) if more else None,
    )


@router.get("/{ticket_id}")
async def get_ticket(
    ticket_id: uuid.UUID, customer: CustomerDep, session: SessionDep, container: ContainerDep
) -> TicketOut:
    ticket = await _ticket_or_404(session, ticket_id, customer)
    await recover_on_read(container, session, ticket)
    return _ticket_out(ticket)


@router.get("/{ticket_id}/messages")
async def list_messages(
    ticket_id: uuid.UUID,
    customer: CustomerDep,
    session: SessionDep,
    container: ContainerDep,
    after: uuid.UUID | None = None,
) -> Page[MessageOut]:
    ticket = await _ticket_or_404(session, ticket_id, customer)
    await recover_on_read(container, session, ticket)
    messages = MessageRepository(session)
    found: Sequence[Message]
    if after is None:
        found = await messages.list_for_ticket(ticket.id)
    else:
        anchor = await messages.get_in_ticket(ticket.id, after)
        if anchor is None:
            raise ProblemError(404, "not_found", "Message not found in this ticket.")
        found = await messages.list_after(ticket.id, anchor)
    return Page[MessageOut](items=[await _message_out(session, message) for message in found])


@router.post("/{ticket_id}/request-human")
async def request_human(
    ticket_id: uuid.UUID,
    idempotency_key: IdempotencyKey,  # required by the contract; the request is idempotent by state
    customer: CustomerDep,
    session: SessionDep,
) -> TicketOut:
    """Immediate handoff with reason `customer_request`. Repeating it changes nothing: a ticket
    that already waits for a human (`handed_off`, `pending_approval`) is returned as it is."""
    ticket = await _ticket_or_404(session, ticket_id, customer)
    if ticket.status == TicketStatus.CLOSED:
        raise ProblemError(409, "ticket_closed", "This ticket is closed. Start a new one.")
    if ticket.status in (TicketStatus.PROCESSING, TicketStatus.ANSWERED):
        ticket.last_route = Route.HANDOFF
        ticket.route_reasons = ["customer_request"]
        await _change_status_or_409(session, ticket, TicketStatus.HANDED_OFF)
        await record(
            session,
            actor_type=ActorType.CUSTOMER,
            actor_id=customer.id,
            action="route.decided",
            entity_type="ticket",
            entity_id=ticket.id,
            details={"route": Route.HANDOFF.value, "reasons": ["customer_request"]},
        )
        await session.commit()
    return _ticket_out(ticket)


@router.post("/{ticket_id}/feedback", status_code=201)
async def give_feedback(
    ticket_id: uuid.UUID,
    body: FeedbackIn,
    response: Response,
    customer: CustomerDep,
    session: SessionDep,
) -> FeedbackOut:
    """One rating per AI message. A repeated request (a double click) returns the first rating
    with 200 and stores nothing; the message row is locked, so two concurrent requests cannot
    both insert."""
    ticket = await _ticket_or_404(session, ticket_id, customer)
    message = await MessageRepository(session).get_in_ticket(
        ticket.id, body.message_id, for_update=True
    )
    if message is None:
        raise ProblemError(404, "not_found", "Message not found in this ticket.")
    if not message.is_ai_generated:
        raise ProblemError(422, "not_ai_message", "Only answers of the AI assistant can be rated.")
    feedbacks = FeedbackRepository(session)
    existing = await feedbacks.get_for_message(message.id)
    if existing is not None:
        existing_id = existing.id  # read first: the rollback expires every loaded object
        await session.rollback()  # releases the row lock; nothing was written
        response.status_code = 200
        return FeedbackOut(id=existing_id)

    # The comment is customer text like a message: masked before it is stored.
    masked = mask(body.comment) if body.comment else None
    try:
        feedback = await feedbacks.add(
            ticket_id=ticket.id,
            message_id=message.id,
            rating=body.rating,
            comment=masked.text if masked else None,
        )
        feedback_id = feedback.id
        await record(
            session,
            actor_type=ActorType.CUSTOMER,
            actor_id=customer.id,
            action="feedback.created",
            entity_type="message",
            entity_id=message.id,
            details={"feedback_id": feedback_id, "rating": body.rating.value},
        )
        if masked is not None:
            await audit_masking(session, masked, entity_type="feedback", entity_id=feedback_id)
        await session.commit()
    except IntegrityError:
        # Another writer stored the first rating (unique `feedback.message_id`, migration
        # 0014): answer like a repeat, with nothing written.
        await session.rollback()
        first = await feedbacks.get_for_message(body.message_id)
        if first is None:
            raise
        response.status_code = 200
        return FeedbackOut(id=first.id)
    return FeedbackOut(id=feedback_id)
