"""SQLAlchemy models of the Postgres tables (docs/data-model.md §1): phase 2, `llm_calls` (P3-01),
`kb_documents` and `kb_chunks` (P2-09).

Conventions from the data model: primary keys are UUID v7 made in the application, timestamps
are `timestamptz`, enums are text with a `CHECK` constraint, every table has `created_at`.
The schema itself is created by the Alembic migrations in `infra/alembic/`, never from these
models; a test compares the two.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Connection,
    Dialect,
    ForeignKey,
    Index,
    MetaData,
    Numeric,
    Text,
    event,
    func,
    inspect,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Mapper,
    ORMExecuteState,
    Session,
    SessionTransaction,
    mapped_column,
    object_session,
)
from sqlalchemy.types import TypeDecorator
from uuid_utils.compat import uuid7

from hub.domain.enums import (
    ActorType,
    Channel,
    CustomerTier,
    DraftStatus,
    FeedbackRating,
    Intent,
    LlmCallContext,
    LlmCallMode,
    LlmCallStatus,
    LlmNode,
    MemoryKind,
    MessageRole,
    OperatorRole,
    RejectReason,
    Resolution,
    Route,
    RunStatus,
    SettingKey,
    TicketStatus,
    ToolCallStatus,
    ToolNode,
    Urgency,
)
from hub.domain.kb import Product, Visibility
from hub.domain.ticket_status import INITIAL_STATUS

# Every constraint gets a predictable name, so later migrations can alter or drop it.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class TextEnum[E: StrEnum](TypeDecorator[E]):
    """A `StrEnum` stored as plain text. Not a Postgres ENUM type: adding a value is a new
    `CHECK` constraint, not an `ALTER TYPE`. The constraint comes from `enum_check`."""

    impl = Text
    cache_ok = True

    def __init__(self, enum: type[E]) -> None:
        super().__init__()
        self.enum = enum

    def process_bind_param(self, value: E | str | None, dialect: Dialect) -> str | None:
        # Through the enum, so a wrong value fails here and not only in the database.
        return None if value is None else self.enum(value).value

    def process_result_value(self, value: str | None, dialect: Dialect) -> E | None:
        return None if value is None else self.enum(value)


def enum_check(column: str, enum: type[StrEnum]) -> CheckConstraint:
    """`CHECK (column IN (...))` named `ck_<table>_<column>`. A NULL value passes the check;
    whether the column may be NULL is decided by its `nullable`."""
    values = ", ".join(f"'{member.value}'" for member in enum)
    return CheckConstraint(f"{column} IN ({values})", name=column)


def new_id() -> uuid.UUID:
    return uuid7()


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy reads this class attribute
        datetime: TIMESTAMP(timezone=True),
        uuid.UUID: UUID(as_uuid=True),
        str: Text(),
        dict[str, Any]: JSONB(),
        list[Any]: JSONB(),
    }


def _id_column() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, default=new_id)


def _created_at_column() -> Mapped[datetime]:
    return mapped_column(server_default=func.now())


class Customer(Base):
    """Mirror of the demo customers of the core mock: only what the Hub needs."""

    __tablename__ = "customers"
    __table_args__ = (enum_check("tier", CustomerTier),)

    id: Mapped[uuid.UUID] = _id_column()
    external_ref: Mapped[str] = mapped_column(unique=True)
    display_name: Mapped[str]
    email: Mapped[str]
    tier: Mapped[CustomerTier] = mapped_column(TextEnum(CustomerTier))
    country: Mapped[str]
    anonymised_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = _created_at_column()


class Operator(Base):
    __tablename__ = "operators"
    __table_args__ = (
        enum_check("role", OperatorRole),
        CheckConstraint("failed_logins >= 0", name="failed_logins"),
        CheckConstraint("session_version >= 1", name="session_version"),
    )

    id: Mapped[uuid.UUID] = _id_column()
    email: Mapped[str] = mapped_column(unique=True)
    name: Mapped[str]
    password_hash: Mapped[str]
    role: Mapped[OperatorRole] = mapped_column(TextEnum(OperatorRole))
    is_active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    last_login_at: Mapped[datetime | None]
    # Lockout after repeated wrong passwords (security §4), kept in the database so that it
    # survives a restart or a scale-to-zero (migration 0004).
    failed_logins: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    locked_until: Mapped[datetime | None]
    # Written into the signed session cookie; logout and a password change increase it, which
    # ends every session of this operator (`hub/auth/operator_session.py`).
    session_version: Mapped[int] = mapped_column(default=1, server_default=text("1"))
    created_at: Mapped[datetime] = _created_at_column()


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        enum_check("channel", Channel),
        enum_check("status", TicketStatus),
        enum_check("intent", Intent),
        enum_check("secondary_intent", Intent),
        enum_check("urgency", Urgency),
        enum_check("last_route", Route),
        enum_check("resolution", Resolution),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="confidence"),
        CheckConstraint("jsonb_typeof(route_reasons) = 'array'", name="route_reasons"),
        Index("ix_tickets_status_urgency_created_at", "status", "urgency", "created_at"),
        Index("ix_tickets_customer_id_created_at", "customer_id", "created_at"),
        Index("ix_tickets_assigned_operator_id_status", "assigned_operator_id", "status"),
    )

    id: Mapped[uuid.UUID] = _id_column()
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"))
    channel: Mapped[Channel] = mapped_column(TextEnum(Channel), default=Channel.CHAT)
    status: Mapped[TicketStatus] = mapped_column(TextEnum(TicketStatus), default=INITIAL_STATUS)
    intent: Mapped[Intent | None] = mapped_column(TextEnum(Intent))
    secondary_intent: Mapped[Intent | None] = mapped_column(TextEnum(Intent))
    urgency: Mapped[Urgency | None] = mapped_column(TextEnum(Urgency))
    language: Mapped[str | None]
    summary: Mapped[str | None]
    last_route: Mapped[Route | None] = mapped_column(TextEnum(Route))
    route_reasons: Mapped[list[Any]] = mapped_column(
        default=list, server_default=text("'[]'::jsonb")
    )
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    assigned_operator_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("operators.id"))
    ai_disclosed_at: Mapped[datetime]
    first_response_at: Mapped[datetime | None]
    resolved_at: Mapped[datetime | None]
    resolution: Mapped[Resolution | None] = mapped_column(TextEnum(Resolution))
    version: Mapped[int] = mapped_column(server_default=text("1"))
    created_at: Mapped[datetime] = _created_at_column()

    # Optimistic lock: every ORM UPDATE checks and increments `version` (StaleDataError when
    # another transaction changed the row first).
    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012 - SQLAlchemy mapper option


# Status changes go only through `TicketRepository.change_status`, which checks the state diagram
# and then records the approved target here (P2-02 review). Any other way of changing the
# status - setting the attribute and calling `save()`, `flush()` or `commit()` - is refused at
# flush time, so a forgotten check cannot slip through. Keyed by ticket id in `Session.info`,
# which belongs to the session of the change and needs no extra attribute on the model.
APPROVED_STATUS_KEY = "hub.approved_ticket_status"


class StatusChangeRefusedError(Exception):
    """A ticket status was changed without `TicketRepository.change_status`."""


def approve_status_change(session: Session, ticket_id: uuid.UUID, target: TicketStatus) -> None:
    """Record that `change_status` checked this change. Only inside an open transaction: the
    approval is dropped when that transaction commits or rolls back, so it cannot outlive it."""
    if not session.in_transaction():
        raise RuntimeError("a status change can only be approved inside a transaction")
    session.info.setdefault(APPROVED_STATUS_KEY, {})[ticket_id] = target


@event.listens_for(Ticket, "before_insert")
def _refuse_initial_status(
    _mapper: Mapper[Ticket], _connection: Connection, target: Ticket
) -> None:
    # None: the column default (INITIAL_STATUS) applies.
    if target.status is not None and target.status != INITIAL_STATUS:
        raise StatusChangeRefusedError(f"a new ticket starts in {INITIAL_STATUS}")


@event.listens_for(Ticket, "before_update")
def _refuse_unapproved_status(
    _mapper: Mapper[Ticket], _connection: Connection, target: Ticket
) -> None:
    if not inspect(target).attrs.status.history.has_changes():
        return
    session = object_session(target)
    approved = {} if session is None else session.info.get(APPROVED_STATUS_KEY, {})
    if approved.pop(target.id, None) != target.status:
        raise StatusChangeRefusedError(
            f"ticket {target.id}: change the status with TicketRepository.change_status"
        )


def _forget_approvals(session: Session) -> None:
    session.info.pop(APPROVED_STATUS_KEY, None)


# An approval lives only until the end of the transaction it was made in. Without this, an
# approval left by a failed flush (change_status → error → rollback) would let a later direct
# status change through in the same session. `after_soft_rollback` fires for every rollback,
# savepoints and the flush's own inner rollback included.
@event.listens_for(Session, "after_soft_rollback")
def _forget_approvals_on_rollback(session: Session, _previous: SessionTransaction) -> None:
    _forget_approvals(session)


@event.listens_for(Session, "after_commit")
def _forget_approvals_on_commit(session: Session) -> None:
    _forget_approvals(session)


class BulkTicketWriteRefusedError(StatusChangeRefusedError):
    """An INSERT, UPDATE or DELETE statement on `tickets` was run with `Session.execute`."""


@event.listens_for(Session, "do_orm_execute")
def _refuse_bulk_ticket_writes(state: ORMExecuteState) -> None:
    """The mapper events above do not see statements run with `Session.execute`:
    `update(Ticket).values(status=...)` would change a status without `change_status`, skip the
    `version` check, and an `insert(Ticket)` could start a ticket in any status (P2-06 review).
    So every DML statement on `tickets` through a session is refused; ticket rows change only
    through the ORM unit of work. Many tickets (e.g. `close-stale`, P8-05) are changed one by
    one with `change_status`.

    `do_orm_execute` fires for every `Session.execute`, Core `update(tickets_table)` included.
    Plain SQL text cannot be inspected and is out of reach; the app never writes tickets that way.
    """
    if not (state.is_insert or state.is_update or state.is_delete):
        return
    statement = state.statement
    # `select(Ticket).from_statement(update(Ticket)...)` wraps the DML statement.
    dml = getattr(statement, "element", statement)
    # By name: an ORM statement carries an annotated copy of the table, not the table object.
    table = getattr(dml, "table", None)
    if getattr(table, "name", None) == Ticket.__tablename__:
        raise BulkTicketWriteRefusedError(
            "tickets are written through the ORM only; change a status with "
            "TicketRepository.change_status"
        )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        enum_check("role", MessageRole),
        CheckConstraint("jsonb_typeof(citations) = 'array'", name="citations"),
        CheckConstraint("jsonb_typeof(masked_entities) = 'object'", name="masked_entities"),
        Index("ix_messages_ticket_id_created_at", "ticket_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _id_column()
    ticket_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tickets.id"))
    role: Mapped[MessageRole] = mapped_column(TextEnum(MessageRole))
    # Card data is already masked here (G1); the raw text never reaches this column.
    body: Mapped[str]
    is_ai_generated: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    author_operator_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("operators.id"))
    # `use_alter`: `runs.trigger_message_id` points back at `messages`, so the two tables form a
    # cycle; SQLAlchemy adds this key after both tables exist (migration 0007 does the same).
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL", use_alter=True)
    )
    citations: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    masked_entities: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )
    idempotency_key: Mapped[str | None] = mapped_column(unique=True)
    created_at: Mapped[datetime] = _created_at_column()


class CustomerMemory(Base):
    """Memory between tickets (ai-pipeline §9, ADR-0012, P6-05): one short summary per closed
    ticket. Masked, no card data, no compliance reasons; erased with the customer's data."""

    __tablename__ = "customer_memory"
    __table_args__ = (
        enum_check("kind", MemoryKind),
        CheckConstraint("char_length(content) <= 400", name="content_length"),
        Index("ix_customer_memory_customer_id_created_at", "customer_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _id_column()
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"))
    # One summary per ticket: `closed` is final, so a ticket is summarised once.
    ticket_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tickets.id"), unique=True)
    kind: Mapped[MemoryKind] = mapped_column(TextEnum(MemoryKind))
    content: Mapped[str]
    expires_at: Mapped[datetime] = mapped_column(
        server_default=text("now() + interval '12 months'")
    )
    created_at: Mapped[datetime] = _created_at_column()


class ToolCall(Base):
    """One tool call of a run, allowed or denied by the permissions (G6, P7-03). Arguments and
    the result summary are masked (G1); a denied call keeps only its argument names."""

    __tablename__ = "tool_calls"
    __table_args__ = (
        enum_check("status", ToolCallStatus),
        enum_check("node", ToolNode),
        CheckConstraint("jsonb_typeof(arguments) = 'object'", name="arguments"),
        CheckConstraint("latency_ms >= 0", name="latency_ms"),
        # The research limit counts the calls of one node in one run (P7-04).
        Index("ix_tool_calls_run_id_node", "run_id", "node"),
    )

    id: Mapped[uuid.UUID] = _id_column()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"))
    node: Mapped[ToolNode] = mapped_column(TextEnum(ToolNode))
    tool: Mapped[str]
    arguments: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )
    result_summary: Mapped[str | None]
    status: Mapped[ToolCallStatus] = mapped_column(TextEnum(ToolCallStatus))
    side_effect: Mapped[bool]
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("operators.id"))
    # Set on successful side-effect calls only: a retry after an error reuses the key.
    idempotency_key: Mapped[str | None] = mapped_column(unique=True)
    external_ref: Mapped[str | None]
    latency_ms: Mapped[int]
    created_at: Mapped[datetime] = _created_at_column()


class Setting(Base):
    """Key-value runtime settings. Only the keys of `SettingKey` exist."""

    __tablename__ = "settings"
    __table_args__ = (enum_check("key", SettingKey),)

    key: Mapped[SettingKey] = mapped_column(TextEnum(SettingKey), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB())
    created_at: Mapped[datetime] = _created_at_column()
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (enum_check("rating", FeedbackRating),)

    id: Mapped[uuid.UUID] = _id_column()
    ticket_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tickets.id"))
    # One rating per message (migration 0014); the API also locks the message row.
    message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("messages.id"), unique=True)
    rating: Mapped[FeedbackRating] = mapped_column(TextEnum(FeedbackRating))
    comment: Mapped[str | None]
    created_at: Mapped[datetime] = _created_at_column()


class AuditLog(Base):
    """Append-only: the app role has only INSERT and SELECT here (migrations 0001 and 0002).

    Rows are written by `hub/audit` (P2-04), which computes the hash chain. `ts` is the
    `created_at` of this table.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        enum_check("actor_type", ActorType),
        CheckConstraint("jsonb_typeof(details) = 'object'", name="details"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(server_default=func.now())
    actor_type: Mapped[ActorType] = mapped_column(TextEnum(ActorType))
    actor_id: Mapped[str | None]
    action: Mapped[str]
    entity_type: Mapped[str]
    entity_id: Mapped[str]
    details: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )
    prev_hash: Mapped[str]
    hash: Mapped[str] = mapped_column(unique=True)


class LlmCall(Base):
    """One model call attempt, written by the LLM gateway (P3-01). Source of truth for cost.

    Request and response bodies are never stored here (data model §1); `error` holds only the
    error class and status code, never provider text, which can echo the request.
    """

    __tablename__ = "llm_calls"
    __table_args__ = (
        enum_check("context", LlmCallContext),
        enum_check("mode", LlmCallMode),
        enum_check("node", LlmNode),
        enum_check("status", LlmCallStatus),
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND cached_tokens >= 0", name="tokens"
        ),
        CheckConstraint("cost_usd >= 0", name="cost_usd"),
        CheckConstraint("latency_ms >= 0", name="latency_ms"),
        Index("ix_llm_calls_created_at", "created_at"),
        Index("ix_llm_calls_run_id", "run_id"),
    )

    id: Mapped[uuid.UUID] = _id_column()
    # SET NULL: deleting an old run (retention) never deletes the cost record.
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    context: Mapped[LlmCallContext] = mapped_column(TextEnum(LlmCallContext))
    # Only `real` rows count against the daily budget (G8, P3-06).
    mode: Mapped[LlmCallMode] = mapped_column(TextEnum(LlmCallMode))
    node: Mapped[LlmNode] = mapped_column(TextEnum(LlmNode))
    model: Mapped[str]
    # Null for embeddings, which use no prompt file.
    prompt_version: Mapped[str | None]
    input_tokens: Mapped[int]
    output_tokens: Mapped[int]
    cached_tokens: Mapped[int]
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6))
    latency_ms: Mapped[int]
    status: Mapped[LlmCallStatus] = mapped_column(TextEnum(LlmCallStatus))
    error: Mapped[str | None]
    created_at: Mapped[datetime] = _created_at_column()


class Run(Base):
    """One execution of the workflow for one customer message (data model §1). Written by the
    workflow graph (`hub.graph.run` and its nodes, P6-01)."""

    __tablename__ = "runs"
    __table_args__ = (
        enum_check("status", RunStatus),
        enum_check("route", Route),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="confidence"),
        CheckConstraint("jsonb_typeof(route_reasons) = 'array'", name="route_reasons"),
        CheckConstraint("jsonb_typeof(features) = 'object'", name="features"),
        CheckConstraint("jsonb_typeof(prompt_versions) = 'object'", name="prompt_versions"),
        CheckConstraint("jsonb_typeof(models) = 'object'", name="models"),
        CheckConstraint(
            "total_cost_usd >= 0 AND input_tokens >= 0 AND output_tokens >= 0", name="usage"
        ),
        CheckConstraint("latency_ms >= 0", name="latency_ms"),
        Index("ix_runs_ticket_id_started_at", "ticket_id", "started_at"),
        Index("ix_runs_status", "status"),
    )

    id: Mapped[uuid.UUID] = _id_column()
    ticket_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tickets.id"))
    # SET NULL: erasure deletes the customer's messages but keeps runs (security §6).
    trigger_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL")
    )
    status: Mapped[RunStatus] = mapped_column(TextEnum(RunStatus))
    route: Mapped[Route | None] = mapped_column(TextEnum(Route))
    route_reasons: Mapped[list[Any]] = mapped_column(
        default=list, server_default=text("'[]'::jsonb")
    )
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    features: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )
    graph_version: Mapped[str]
    # node -> version without the node prefix, e.g. {"triage": "v1"} (data model §1).
    prompt_versions: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )
    # role -> model id used
    models: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'::jsonb"))
    total_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), default=Decimal(0), server_default=text("0")
    )
    input_tokens: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    latency_ms: Mapped[int | None]
    # An error class only, never a message text (it can echo customer input).
    error: Mapped[str | None]
    trace_id: Mapped[str | None]
    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None]


class Draft(Base):
    """The AI draft of a run that waits for an operator (data model §1)."""

    __tablename__ = "drafts"
    __table_args__ = (
        enum_check("status", DraftStatus),
        enum_check("reject_reason", RejectReason),
        CheckConstraint("groundedness BETWEEN 0 AND 1", name="groundedness"),
        CheckConstraint("edit_ratio BETWEEN 0 AND 1", name="edit_ratio"),
        CheckConstraint("jsonb_typeof(citations) = 'array'", name="citations"),
        CheckConstraint("jsonb_typeof(evidence) = 'object'", name="evidence"),
        CheckConstraint("jsonb_typeof(proposed_actions) = 'array'", name="proposed_actions"),
        CheckConstraint(
            "approved_actions IS NULL OR jsonb_typeof(approved_actions) = 'array'",
            name="approved_actions",
        ),
        CheckConstraint("jsonb_typeof(unsupported_claims) = 'array'", name="unsupported_claims"),
        Index("ix_drafts_ticket_id", "ticket_id"),
    )

    id: Mapped[uuid.UUID] = _id_column()
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), unique=True
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tickets.id"))
    can_answer: Mapped[bool]
    missing_information: Mapped[str | None]
    # Model output after `OutputMask` (hub/graph/draft.py): placeholders restored, then card
    # data masked (G1). Never stored unmasked.
    body: Mapped[str]
    citations: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    evidence: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )
    proposed_actions: Mapped[list[Any]] = mapped_column(
        default=list, server_default=text("'[]'::jsonb")
    )
    groundedness: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    unsupported_claims: Mapped[list[Any]] = mapped_column(
        default=list, server_default=text("'[]'::jsonb")
    )
    status: Mapped[DraftStatus] = mapped_column(TextEnum(DraftStatus))
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("operators.id"))
    reviewed_at: Mapped[datetime | None]
    final_body: Mapped[str | None]
    edit_ratio: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    reject_reason: Mapped[RejectReason | None] = mapped_column(TextEnum(RejectReason))
    reject_note: Mapped[str | None]
    # The ids of the proposed actions the operator ticked (P7-05); `act` runs them. Null for a
    # draft written before migration 0013 (compared as "no actions").
    approved_actions: Mapped[list[Any] | None] = mapped_column(
        default=list, server_default=text("'[]'::jsonb")
    )
    # The operator-scoped `Idempotency-Key` of the review, also of a reject without a reply
    # (P7-05): a repeated request is compared with what the draft stored.
    review_key: Mapped[str | None] = mapped_column(unique=True)
    created_at: Mapped[datetime] = _created_at_column()


class KbDocument(Base):
    """One knowledge-base file of `data/kb/`, written only by `make kb-load` (P2-09).

    `id` is the slug of the file name, or `error-codes` for `error_codes.csv`. `version` counts
    the changes of the file's SHA-256 (`content_hash`).
    """

    __tablename__ = "kb_documents"
    __table_args__ = (
        enum_check("product", Product),
        enum_check("visibility", Visibility),
        CheckConstraint("jsonb_typeof(intents) = 'array'", name="intents"),
        CheckConstraint("version >= 1", name="version"),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    title: Mapped[str]
    product: Mapped[Product] = mapped_column(TextEnum(Product))
    visibility: Mapped[Visibility] = mapped_column(TextEnum(Visibility))
    intents: Mapped[list[Any]]
    url: Mapped[str]
    content_hash: Mapped[str]
    version: Mapped[int]
    updated_at: Mapped[datetime]
    created_at: Mapped[datetime] = _created_at_column()


class KbChunk(Base):
    """One section of an article, or one error-code row (ADR-0006). Id: `<doc_id>#<ordinal>`,
    for error codes `error-codes#<code>` (data model §1)."""

    __tablename__ = "kb_chunks"
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="ordinal"),
        CheckConstraint("token_count >= 0", name="token_count"),
        Index("ix_kb_chunks_doc_id", "doc_id"),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    doc_id: Mapped[str] = mapped_column(ForeignKey("kb_documents.id", ondelete="CASCADE"))
    ordinal: Mapped[int]
    section: Mapped[str]
    text: Mapped[str]
    token_count: Mapped[int]
    content_hash: Mapped[str]
    created_at: Mapped[datetime] = _created_at_column()
