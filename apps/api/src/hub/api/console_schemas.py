"""Request and response models of the auth and console endpoints (docs/api.md §3).

The operator view holds internal fields (route reasons, masking counts, citations with chunk
ids). It is never returned by a customer endpoint.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints

from hub.api.schemas import MessageText
from hub.domain.draft import IssueType
from hub.domain.enums import (
    CustomerTier,
    DraftStatus,
    Intent,
    LlmCallStatus,
    LlmNode,
    MessageRole,
    OperatorRole,
    RejectReason,
    Route,
    RunStatus,
    TicketStatus,
    ToolCallStatus,
    ToolNode,
    Urgency,
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- auth -----------------------------------------------------------------------------------


class LoginIn(_Strict):
    # 254: the longest valid email address. No format check: an unknown address is just a
    # failed login, and a format error would tell an attacker nothing useful either.
    email: Annotated[str, StringConstraints(min_length=1, max_length=254)]
    # Argon2 hashes any length; the cap keeps a huge password from costing hashing time.
    password: Annotated[str, StringConstraints(min_length=1, max_length=1024)]


class OperatorOut(BaseModel):
    id: uuid.UUID
    email: str
    name: str
    role: OperatorRole


class OperatorSessionOut(BaseModel):
    """Answer of `POST /auth/login` and `GET /auth/me`. The SPA sends `csrf_token` in the
    `X-CSRF-Token` header of every state-changing console request."""

    operator: OperatorOut
    csrf_token: str


# --- console --------------------------------------------------------------------------------


class TicketVersionIn(_Strict):
    """Body of claim, release and close: the `version` the console last saw (api.md §3,
    conflict rule). A changed ticket answers `409 stale_draft`."""

    ticket_version: Annotated[int, Field(ge=1)]


class ReplyIn(_Strict):
    ticket_version: Annotated[int, Field(ge=1)]
    # The same limit as a customer message; masked (G1) before it is stored.
    body: MessageText


# Ids of the draft's proposed actions the operator ticked ("a1", ...): checked against the draft,
# stored with the review and run by `act` after it (P7-05).
ActionIds = Annotated[
    list[Annotated[str, StringConstraints(pattern=r"^a[0-9]{1,3}$")]], Field(max_length=20)
]
# Same limit as a customer comment on feedback; masked (G1) before it is stored.
RejectNote = Annotated[str, StringConstraints(max_length=1000, pattern=r"^[^\x00]*$")]


class IssueParams(_Strict):
    """A Jira issue an operator creates by hand (P7-05). Lengths as on the MCP server; the texts
    are masked (G1) before they are sent. The customer and the ticket come from the ticket row,
    never from the request."""

    issue_type: IssueType
    # NUL refused (api.md §1): Postgres jsonb (`tool_calls.arguments`) cannot store it (P8-04).
    summary: Annotated[str, StringConstraints(min_length=1, max_length=255, pattern=r"^[^\x00]*$")]
    description: Annotated[
        str, StringConstraints(min_length=1, max_length=5000, pattern=r"^[^\x00]*$")
    ]


class ActionIn(_Strict):
    """`POST /console/tickets/{id}/actions`: one tool action run by an operator (api.md §3)."""

    type: Literal["jira_create_issue"]
    params: IssueParams


class ApproveIn(_Strict):
    ticket_version: Annotated[int, Field(ge=1)]
    approved_actions: ActionIds = Field(default_factory=list)


class EditIn(_Strict):
    ticket_version: Annotated[int, Field(ge=1)]
    # The same limit as a customer message; masked (G1) before it is stored.
    body: MessageText
    approved_actions: ActionIds = Field(default_factory=list)


class RejectIn(_Strict):
    ticket_version: Annotated[int, Field(ge=1)]
    reason: RejectReason
    note: RejectNote | None = None
    # With a reply the operator's own answer is sent; without it the ticket stays handed off.
    reply_body: MessageText | None = None


class ConsoleCustomerOut(BaseModel):
    id: uuid.UUID
    customer_ref: str
    display_name: str
    tier: CustomerTier
    country: str


class AssigneeOut(BaseModel):
    id: uuid.UUID
    name: str


class QueueTicketOut(BaseModel):
    """One row of the operator queue; also the answer of claim, release, reply and close."""

    id: uuid.UUID
    status: TicketStatus
    intent: Intent | None
    urgency: Urgency | None
    summary: str | None
    last_route: Route | None
    route_reasons: list[str]
    customer: ConsoleCustomerOut
    assigned_operator: AssigneeOut | None
    # The last customer message (the ticket's creation when there is none).
    waiting_since: datetime
    created_at: datetime
    version: int


class ConsoleMessageOut(BaseModel):
    id: uuid.UUID
    role: MessageRole
    author_label: str
    is_ai_generated: bool
    author_operator_id: uuid.UUID | None
    # As stored: card data is already masked.
    body: str
    citations: list[dict[str, Any]]
    masked_entities: dict[str, int]
    created_at: datetime


class RunStepOut(BaseModel):
    """One node of the run with its duration (data model, `runs.steps`; P9-01). A resumed run
    lists `human_review` twice: `paused`, then `ok` after the operator's decision."""

    node: str
    started_at: datetime
    duration_ms: int
    outcome: Literal["ok", "error", "timeout", "paused"]


class RunOut(BaseModel):
    """The latest run of a ticket: status, route and what it cost (data model, `runs`)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: RunStatus
    route: Route | None
    route_reasons: list[str]
    confidence: Decimal | None
    prompt_versions: dict[str, str]
    models: dict[str, str]
    total_cost_usd: Decimal
    input_tokens: int
    output_tokens: int
    latency_ms: int | None
    # An error class only, never a message text.
    error: str | None
    # The Langfuse trace; null when tracing was off or the run was not sampled (P9-01).
    trace_id: str | None
    # Empty for a run from before P9-01.
    steps: list[RunStepOut]
    started_at: datetime
    finished_at: datetime | None


class DraftOut(BaseModel):
    """The draft of the latest run (data model, `drafts`). `body` and `final_body` are stored
    card-masked; `evidence` holds what the operator checks the draft against."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    run_id: uuid.UUID
    status: DraftStatus
    can_answer: bool
    missing_information: str | None
    body: str
    citations: list[dict[str, Any]]
    evidence: dict[str, Any]
    proposed_actions: list[dict[str, Any]]
    groundedness: Decimal | None
    unsupported_claims: list[Any]
    reviewed_by: uuid.UUID | None
    reviewed_at: datetime | None
    final_body: str | None
    edit_ratio: Decimal | None
    reject_reason: RejectReason | None
    reject_note: str | None
    # The proposed actions the operator ticked (P7-05); `act` runs them after the review. A
    # draft from before migration 0013 has none stored (null), shown as an empty list.
    approved_actions: Annotated[list[str], BeforeValidator(lambda value: value or [])]
    created_at: datetime


class DraftReviewOut(BaseModel):
    """Answer of approve, edit and reject: the reviewed draft, the message that was sent (none
    for a reject without a reply) and the ticket's queue row with its new `version`."""

    draft: DraftOut
    message: ConsoleMessageOut | None
    ticket: QueueTicketOut


class MemoryOut(BaseModel):
    """A memory summary of an earlier ticket of the same customer (P6-05, ui.md "History")."""

    ticket_id: uuid.UUID
    intent: Intent | None
    summary: str
    created_at: datetime
    expires_at: datetime


class ToolCallOut(BaseModel):
    """One tool call of the ticket's runs (data model, `tool_calls`; P7-05): arguments and
    summary are stored masked; `external_ref` is the Jira key or the Slack message `ts`."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    run_id: uuid.UUID
    node: ToolNode
    tool: str
    status: ToolCallStatus
    side_effect: bool
    arguments: dict[str, Any]
    result_summary: str | None
    approved_by: uuid.UUID | None
    external_ref: str | None
    latency_ms: int
    created_at: datetime


class ModelCallOut(BaseModel):
    """One model call attempt of the latest run (data model, `llm_calls`): no text, only what the
    Run tab shows (P10-03 review). Per-step durations are in `RunOut.steps` (P9-01)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    node: LlmNode
    model: str
    prompt_version: str | None
    status: LlmCallStatus
    latency_ms: int
    cost_usd: Decimal
    input_tokens: int
    output_tokens: int
    created_at: datetime


class ConsoleTicketOut(QueueTicketOut):
    """`GET /console/tickets/{id}`. Fields of later phases are present and empty until then:
    evidence chunks (phase 5; the B0 cited documents are in `draft.evidence`). `trace_url`
    (P9-01): the latest run's trace in Langfuse, null without a trace id or without
    `LANGFUSE_PROJECT_ID`. `memory` (P6-05): the customer's memory items that have not
    expired, newest first, at most 5, without this ticket's own."""

    language: str | None
    confidence: Decimal | None
    ai_disclosed_at: datetime
    first_response_at: datetime | None
    resolved_at: datetime | None
    resolution: str | None
    messages: list[ConsoleMessageOut]
    latest_run: RunOut | None
    # The draft of the latest run, whatever its status.
    draft: DraftOut | None
    evidence: list[Any] = Field(default_factory=list)
    memory: list[MemoryOut] = Field(default_factory=list)
    # Every tool call of the ticket's runs, oldest first (P7-05).
    tool_calls: list[ToolCallOut] = Field(default_factory=list)
    # The model call attempts of the latest run, oldest first (P10-03 review).
    model_calls: list[ModelCallOut] = Field(default_factory=list)
    trace_url: str | None = None


# --- account view (P10-03 review) -------------------------------------------------------------


class LimitOut(BaseModel):
    name: str
    limit_eur: Decimal
    # Per currency, not converted (the core mock has no exchange rates).
    used: dict[str, Decimal]


class AccountProfileOut(BaseModel):
    tier: str
    country: str
    # The flag only, never the reason (tipping-off rule, world file §6).
    restricted: bool
    limits: list[LimitOut]
    as_of: date


class CardOut(BaseModel):
    card_ref: str
    kind: str
    last4: str
    status: str
    delivery_status: str | None
    expected_delivery_by: date | None
    delivery_overdue: bool


class TransactionOut(BaseModel):
    reference: str
    type: str
    amount: Decimal
    currency: str
    status: str
    error_code: str | None
    requested_at: datetime


class AccountOut(BaseModel):
    """`GET /console/tickets/{id}/account`: what the core read tools return now for the ticket's
    customer, read through the tool permissions (node `console`). A part is null when its call
    was refused or failed; `unavailable` names those parts, so the console shows them as unknown,
    never as "no" (a null `profile` means the restricted flag is unknown)."""

    profile: AccountProfileOut | None
    cards: list[CardOut] | None
    transactions: list[TransactionOut] | None
    unavailable: list[Literal["profile", "cards", "transactions"]]
