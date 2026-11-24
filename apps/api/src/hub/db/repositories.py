"""Repositories: the queries the API and later the workflow need, one class per table.

A repository works inside the caller's session and never commits: the caller decides where the
transaction ends (validate → mask → save → audit is one transaction in P2-06). Repositories
flush, so database errors (constraints, stale versions) show up at the call that caused them.

Message bodies and feedback comments must already be masked (G1); masking is the caller's step,
before anything reaches this module.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import ColumnElement, case, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.orm.exc import StaleDataError

from hub.db.models import (
    Customer,
    CustomerMemory,
    Draft,
    Feedback,
    LlmCall,
    Message,
    Operator,
    Run,
    Setting,
    Ticket,
    ToolCall,
    approve_status_change,
)
from hub.domain.enums import (
    FeedbackRating,
    Intent,
    MemoryKind,
    MessageRole,
    SettingKey,
    TicketStatus,
    Urgency,
)
from hub.domain.ticket_status import INITIAL_STATUS, check_transition


class ConcurrentUpdateError(Exception):
    """Another transaction changed the row first (optimistic lock on `tickets.version`)."""


class CustomerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, customer: Customer) -> Customer:
        self._session.add(customer)
        await self._session.flush()
        return customer

    async def get(self, customer_id: uuid.UUID) -> Customer | None:
        return await self._session.get(Customer, customer_id)

    async def get_by_external_ref(self, external_ref: str) -> Customer | None:
        result = await self._session.execute(
            select(Customer).where(Customer.external_ref == external_ref)
        )
        return result.scalar_one_or_none()


class OperatorRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, operator: Operator) -> Operator:
        self._session.add(operator)
        await self._session.flush()
        return operator

    async def get(self, operator_id: uuid.UUID) -> Operator | None:
        return await self._session.get(Operator, operator_id)

    async def get_by_email(self, email: str, *, for_update: bool = False) -> Operator | None:
        """Case-insensitive. `for_update` locks the row, so two failed logins at the same time
        both count (`failed_logins`)."""
        statement = select(Operator).where(func.lower(Operator.email) == email.lower())
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def change_password(self, operator: Operator, password_hash: str) -> None:
        """Store a new hash and end every session of the operator (`session_version`)."""
        operator.password_hash = password_hash
        operator.session_version += 1
        await self._session.flush()

    async def end_sessions(self, operator: Operator) -> None:
        """Logout: every cookie issued so far stops working, a copied one too."""
        operator.session_version += 1
        await self._session.flush()


class TicketRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, *, customer_id: uuid.UUID, ai_disclosed_at: datetime) -> Ticket:
        ticket = Ticket(
            customer_id=customer_id, status=INITIAL_STATUS, ai_disclosed_at=ai_disclosed_at
        )
        self._session.add(ticket)
        await self._session.flush()
        return ticket

    async def get(self, ticket_id: uuid.UUID) -> Ticket | None:
        return await self._session.get(Ticket, ticket_id)

    async def list_for_customer(
        self, customer_id: uuid.UUID, *, limit: int, before: uuid.UUID | None = None
    ) -> Sequence[Ticket]:
        """Newest first. Ordered by the UUID v7 id, which grows with creation time, so a page
        cursor is just the last id (`before`)."""
        statement = select(Ticket).where(Ticket.customer_id == customer_id)
        if before is not None:
            statement = statement.where(Ticket.id < before)
        result = await self._session.execute(statement.order_by(Ticket.id.desc()).limit(limit))
        return result.scalars().all()

    async def get_for_customer(self, ticket_id: uuid.UUID, customer_id: uuid.UUID) -> Ticket | None:
        """The ticket only when it belongs to this customer (customer isolation)."""
        result = await self._session.execute(
            select(Ticket).where(Ticket.id == ticket_id, Ticket.customer_id == customer_id)
        )
        return result.scalar_one_or_none()

    async def lock(self, ticket: Ticket) -> Ticket:
        """Lock the ticket row until the transaction ends and reload it from the database.

        A check made before the lock (e.g. "not closed") may be stale by now: another
        transaction can have closed the ticket and committed while this one waited. Callers
        re-check after this call. The same lock as `MessageRepository.lock_ticket`, so a message
        write and a status change on one ticket are serialised."""
        await self._session.execute(
            select(Ticket)
            .where(Ticket.id == ticket.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return ticket

    async def change_status(self, ticket: Ticket, target: TicketStatus) -> Ticket:
        """Move the ticket to `target`. Raises `InvalidTransitionError` before any SQL when the
        state diagram forbids it, `ConcurrentUpdateError` when the stored version moved on.

        The only way to change a status: a flush with any other status change raises
        `StatusChangeRefusedError` (`hub.db.models`)."""
        ticket.status = check_transition(ticket.status, target)
        # connection() begins a transaction when none is open, so the approval below always
        # belongs to one and is dropped with it.
        await self._session.connection()
        approve_status_change(self._session.sync_session, ticket.id, target)
        await self.save(ticket)
        return ticket

    async def save(self, ticket: Ticket) -> None:
        """Flush pending changes (of the ticket and anything else in the session); the version
        check runs here. A changed status is refused unless it came from `change_status`."""
        # Read before the flush: a failed flush expires the object, and reading it then raises
        # PendingRollbackError instead of the error below.
        ticket_id = ticket.id
        try:
            await self._session.flush()
        except StaleDataError as exc:
            raise ConcurrentUpdateError(f"ticket {ticket_id} was changed by someone else") from exc


# Sort rank of urgency in the operator queue: high first. A ticket without urgency (no triage
# yet, phase 2) ranks last.
_URGENCY_RANK = case(
    (Ticket.urgency == Urgency.HIGH, 3),
    (Ticket.urgency == Urgency.NORMAL, 2),
    (Ticket.urgency == Urgency.LOW, 1),
    else_=0,
)


@dataclass(frozen=True)
class QueueFilters:
    """Filters of `GET /console/tickets` (api.md §3). `None` means "no filter"."""

    statuses: frozenset[TicketStatus] | None = None
    intent: Intent | None = None
    urgency: Urgency | None = None
    # A specific operator, or `UNASSIGNED`; `None` = any.
    assigned_to: uuid.UUID | Literal["none"] | None = None
    route_reason: str | None = None
    q: str | None = None
    ticket_id: uuid.UUID | None = None


@dataclass(frozen=True)
class QueueCursor:
    """Keyset position in the queue order: urgency rank desc, waiting since asc, id asc."""

    urgency_rank: int
    waiting_since: datetime
    ticket_id: uuid.UUID


@dataclass(frozen=True)
class QueueRow:
    ticket: Ticket
    customer: Customer
    assignee: Operator | None
    waiting_since: datetime
    urgency_rank: int

    @property
    def cursor(self) -> QueueCursor:
        return QueueCursor(self.urgency_rank, self.waiting_since, self.ticket.id)


def _like_pattern(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class QueueRepository:
    """The operator queue (console list and the row of one ticket)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def page(
        self, filters: QueueFilters, *, limit: int, after: QueueCursor | None = None
    ) -> list[QueueRow]:
        """Default order (api.md §3): urgency desc, waiting time desc. A ticket waits since the
        last customer message (the ticket's creation when it has none)."""
        last_customer_message = (
            select(func.max(Message.created_at))
            .where(Message.ticket_id == Ticket.id, Message.role == MessageRole.CUSTOMER)
            .correlate(Ticket)
            .scalar_subquery()
        )
        waiting_since = func.coalesce(last_customer_message, Ticket.created_at)
        assignee = aliased(Operator)
        statement = (
            select(Ticket, Customer, assignee, waiting_since, _URGENCY_RANK)
            .join(Customer, Customer.id == Ticket.customer_id)
            .outerjoin(assignee, assignee.id == Ticket.assigned_operator_id)
        )
        statement = statement.where(*self._conditions(filters))
        if after is not None:
            # Row comparison: (-rank, waiting, id) ascending is the queue order.
            statement = statement.where(
                tuple_(-_URGENCY_RANK, waiting_since, Ticket.id)
                > tuple_(-after.urgency_rank, after.waiting_since, after.ticket_id)
            )
        statement = statement.order_by(_URGENCY_RANK.desc(), waiting_since, Ticket.id).limit(limit)
        result = await self._session.execute(statement)
        return [
            QueueRow(ticket, customer, operator, waited, rank)
            for ticket, customer, operator, waited, rank in result
        ]

    async def get(self, ticket_id: uuid.UUID) -> QueueRow | None:
        rows = await self.page(QueueFilters(ticket_id=ticket_id), limit=1)
        return rows[0] if rows else None

    @staticmethod
    def _conditions(filters: QueueFilters) -> list[ColumnElement[bool]]:
        conditions: list[ColumnElement[bool]] = []
        if filters.ticket_id is not None:
            conditions.append(Ticket.id == filters.ticket_id)
        if filters.statuses is not None:
            conditions.append(Ticket.status.in_(sorted(filters.statuses)))
        if filters.intent is not None:
            conditions.append(Ticket.intent == filters.intent)
        if filters.urgency is not None:
            conditions.append(Ticket.urgency == filters.urgency)
        if filters.assigned_to == "none":
            conditions.append(Ticket.assigned_operator_id.is_(None))
        elif filters.assigned_to is not None:
            conditions.append(Ticket.assigned_operator_id == filters.assigned_to)
        if filters.route_reason is not None:
            conditions.append(Ticket.route_reasons.contains([filters.route_reason]))
        if filters.q:
            pattern = _like_pattern(filters.q)
            conditions.append(
                or_(
                    Ticket.summary.ilike(pattern, escape="\\"),
                    Customer.display_name.ilike(pattern, escape="\\"),
                    Customer.external_ref.ilike(pattern, escape="\\"),
                )
            )
        return conditions


class MessageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        ticket_id: uuid.UUID,
        role: MessageRole,
        body: str,
        is_ai_generated: bool = False,
        author_operator_id: uuid.UUID | None = None,
        run_id: uuid.UUID | None = None,
        citations: list[Any] | None = None,
        masked_entities: dict[str, int] | None = None,
        idempotency_key: str | None = None,
    ) -> Message:
        await self.lock_ticket(ticket_id)
        if role == MessageRole.CUSTOMER:
            await self._touch_ticket(ticket_id)
        message = Message(
            ticket_id=ticket_id,
            role=role,
            body=body,
            is_ai_generated=is_ai_generated,
            author_operator_id=author_operator_id,
            run_id=run_id,
            citations=citations or [],
            masked_entities=masked_entities or {},
            idempotency_key=idempotency_key,
            # Taken at the INSERT, after the lock above: see `lock_ticket`.
            created_at=func.clock_timestamp(),
        )
        self._session.add(message)
        await self._session.flush()
        return message

    async def lock_ticket(self, ticket_id: uuid.UUID) -> None:
        """Serialise message writes per ticket (P2-06 review, P2-07).

        The chat polls `?after=<last message id>` and gets the messages ordered after it by
        `(created_at, id)`. With `created_at = now()` (the transaction start), a message saved
        by a transaction that began earlier but committed later sorts before an anchor the
        client already has, and the client never sees it. A sequence or `clock_timestamp()`
        alone does not help: both are taken at the INSERT, not at commit.

        So every message write first locks the ticket row (`SELECT ... FOR UPDATE`, held until
        the transaction ends) and only then takes `clock_timestamp()`. A second writer waits for
        the first to commit, so its timestamp is later than every message already visible.
        Called by `add`, so no caller can forget it. Lock order everywhere: ticket row first,
        then the audit advisory lock (`hub/audit/log.py`), so the two cannot deadlock.
        """
        await self._session.execute(
            select(Ticket.id).where(Ticket.id == ticket_id).with_for_update()
        )

    async def _touch_ticket(self, ticket_id: uuid.UUID) -> None:
        """A customer message changes the ticket's `version` (P6-03, from P2-07), also when the
        status stays the same (a message on a `handed_off` or `pending_approval` ticket): an
        operator's approve, edit, reject or reply loaded before it then answers
        `409 stale_draft` (api.md §3, "the customer wrote again"). Under the row lock taken
        just before, in the message's transaction. `flag_modified` makes the unit of work write
        the row although no value changed; the version column moves on with that UPDATE."""
        ticket = await self._session.get(Ticket, ticket_id)
        if ticket is None:  # pragma: no cover - the message's ticket exists (foreign key)
            return
        flag_modified(ticket, "route_reasons")
        try:
            await self._session.flush()
        except StaleDataError as exc:
            raise ConcurrentUpdateError(f"ticket {ticket_id} was changed by someone else") from exc

    async def get_by_idempotency_key(self, key: str) -> Message | None:
        result = await self._session.execute(select(Message).where(Message.idempotency_key == key))
        return result.scalar_one_or_none()

    async def list_for_ticket(self, ticket_id: uuid.UUID) -> Sequence[Message]:
        # created_at is the INSERT time under the ticket lock (`lock_ticket`); the UUID v7 id
        # breaks a tie in creation order.
        result = await self._session.execute(
            select(Message)
            .where(Message.ticket_id == ticket_id)
            .order_by(Message.created_at, Message.id)
        )
        return result.scalars().all()

    async def list_after(self, ticket_id: uuid.UUID, after: Message) -> Sequence[Message]:
        """Messages of the ticket created after `after`, in the order of `list_for_ticket`."""
        result = await self._session.execute(
            select(Message)
            .where(
                Message.ticket_id == ticket_id,
                tuple_(Message.created_at, Message.id) > tuple_(after.created_at, after.id),
            )
            .order_by(Message.created_at, Message.id)
        )
        return result.scalars().all()

    async def get_in_ticket(
        self, ticket_id: uuid.UUID, message_id: uuid.UUID, *, for_update: bool = False
    ) -> Message | None:
        """`for_update` locks the row until the transaction ends (serialises writers that
        depend on this message, e.g. one feedback per message)."""
        statement = select(Message).where(Message.id == message_id, Message.ticket_id == ticket_id)
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()


class RunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def latest_for_ticket(self, ticket_id: uuid.UUID) -> Run | None:
        # UUID v7 ids break a tie of equal start times in creation order.
        result = await self._session.execute(
            select(Run)
            .where(Run.ticket_id == ticket_id)
            .order_by(Run.started_at.desc(), Run.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


class DraftRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, draft_id: uuid.UUID, *, for_update: bool = False) -> Draft | None:
        """`for_update` locks the row and reloads it: a review that waited for the lock sees the
        status another review committed meanwhile."""
        statement = select(Draft).where(Draft.id == draft_id)
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def for_run(self, run_id: uuid.UUID) -> Draft | None:
        result = await self._session.execute(select(Draft).where(Draft.run_id == run_id))
        return result.scalar_one_or_none()

    async def by_review_key(self, review_key: str) -> Draft | None:
        """The draft a review with this operator-scoped `Idempotency-Key` reviewed."""
        result = await self._session.execute(select(Draft).where(Draft.review_key == review_key))
        return result.scalar_one_or_none()

    async def latest_for_ticket(self, ticket_id: uuid.UUID) -> Draft | None:
        result = await self._session.execute(
            select(Draft)
            .where(Draft.ticket_id == ticket_id)
            .order_by(Draft.created_at.desc(), Draft.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


class ToolCallRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def for_ticket(self, ticket_id: uuid.UUID) -> Sequence[ToolCall]:
        """Every tool call of the ticket's runs, oldest first (the console shows them, P7-05)."""
        result = await self._session.execute(
            select(ToolCall)
            .join(Run, Run.id == ToolCall.run_id)
            .where(Run.ticket_id == ticket_id)
            .order_by(ToolCall.created_at, ToolCall.id)
        )
        return result.scalars().all()


class LlmCallRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def for_run(self, run_id: uuid.UUID) -> Sequence[LlmCall]:
        """The model call attempts of one run, oldest first (the console's Run tab, P10-03)."""
        result = await self._session.execute(
            select(LlmCall).where(LlmCall.run_id == run_id).order_by(LlmCall.created_at, LlmCall.id)
        )
        return result.scalars().all()


class SettingsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, key: SettingKey) -> Any:  # noqa: ANN401 - JSON value of any shape
        setting = await self._session.get(Setting, key)
        return None if setting is None else setting.value

    async def get_all(self) -> dict[SettingKey, Any]:
        result = await self._session.execute(select(Setting))
        return {setting.key: setting.value for setting in result.scalars()}

    async def set(self, key: SettingKey, value: Any) -> None:  # noqa: ANN401 - JSON value
        setting = await self._session.get(Setting, key)
        if setting is None:
            self._session.add(Setting(key=key, value=value))
        else:
            setting.value = value
        await self._session.flush()


class FeedbackRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        ticket_id: uuid.UUID,
        message_id: uuid.UUID,
        rating: FeedbackRating,
        comment: str | None = None,
    ) -> Feedback:
        feedback = Feedback(
            ticket_id=ticket_id, message_id=message_id, rating=rating, comment=comment
        )
        self._session.add(feedback)
        await self._session.flush()
        return feedback

    async def get_for_message(self, message_id: uuid.UUID) -> Feedback | None:
        """The first feedback on a message (the API stores at most one)."""
        result = await self._session.execute(
            select(Feedback)
            .where(Feedback.message_id == message_id)
            .order_by(Feedback.created_at, Feedback.id)
            .limit(1)
        )
        return result.scalar_one_or_none()


class MemoryRepository:
    """`customer_memory` (P6-05). Contents must already be masked (G1)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self, *, customer_id: uuid.UUID, ticket_id: uuid.UUID, content: str
    ) -> CustomerMemory:
        memory = CustomerMemory(
            customer_id=customer_id,
            ticket_id=ticket_id,
            kind=MemoryKind.TICKET_SUMMARY,
            content=content,
        )
        self._session.add(memory)
        await self._session.flush()
        return memory

    async def for_ticket(self, ticket_id: uuid.UUID) -> CustomerMemory | None:
        result = await self._session.execute(
            select(CustomerMemory).where(CustomerMemory.ticket_id == ticket_id)
        )
        return result.scalar_one_or_none()

    async def active_for_customer(
        self, customer_id: uuid.UUID, *, limit: int, exclude_ticket: uuid.UUID | None = None
    ) -> Sequence[tuple[CustomerMemory, Intent | None]]:
        """The newest items that have not expired (database time), with the intent of their
        ticket; without the item of `exclude_ticket`."""
        conditions = [
            CustomerMemory.customer_id == customer_id,
            CustomerMemory.expires_at > func.now(),
        ]
        if exclude_ticket is not None:
            conditions.append(CustomerMemory.ticket_id != exclude_ticket)
        result = await self._session.execute(
            select(CustomerMemory, Ticket.intent)
            .join(Ticket, Ticket.id == CustomerMemory.ticket_id)
            .where(*conditions)
            .order_by(CustomerMemory.created_at.desc(), CustomerMemory.id.desc())
            .limit(limit)
        )
        return [(memory, intent) for memory, intent in result.all()]

    async def open_tickets(
        self,
        customer_id: uuid.UUID,
        *,
        exclude: uuid.UUID,
        limit: int,
        created_before: datetime | None = None,
    ) -> Sequence[Ticket]:
        """The customer's tickets that are not closed, other than `exclude`, newest first;
        with `created_before`, only tickets created before that time."""
        conditions = [
            Ticket.customer_id == customer_id,
            Ticket.id != exclude,
            Ticket.status != TicketStatus.CLOSED,
        ]
        if created_before is not None:
            conditions.append(Ticket.created_at < created_before)
        result = await self._session.execute(
            select(Ticket).where(*conditions).order_by(Ticket.id.desc()).limit(limit)
        )
        return result.scalars().all()
