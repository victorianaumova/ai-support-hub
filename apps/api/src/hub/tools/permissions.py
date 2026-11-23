"""Tool permissions (G6, security §3; ai-pipeline §4; ADR-0011).

Every tool call of the Hub is decided here before it reaches the MCP server. Pure: the caller
passes the run's scope, the approval and the counts; `hub.tools.mcp_client` records the
decision and runs the call.

Rules, in the order they are checked:
1. The tool exists. A model may name only a model tool (`registry.MODEL_TOOLS`); code names the
   MCP tool.
2. The node's allow-list holds the tool. Anything else is denied.
3. Limits: calls per node and run (research: 5, `search_kb` included, ai-pipeline §4), counted
   by the caller from the run's `tool_calls` rows (denied ones too, P7-04), and at most one
   successful call of each side-effect tool per ticket (security T8; the server enforces it
   again).
4. Identity: no caller may send `customer_ref`, `ticket_id` or `idempotency_key`. They come
   from the scope only, which the graph builds from the tickets row, so a model can never choose
   whose data it reads. A model that tries is denied, and the denial is recorded and audited
   (the argument name, never its value).
5. The arguments match the tool's argument model (extra fields forbidden).
   The scope's `customer_ref` must be the reference of the scope ticket's own customer, as the
   database says (`ticket_customer_ref`); anything else is `invalid_identity` (P7-04).
6. A side-effect tool runs only with an approval for exactly this tool, this ticket and these
   arguments, of the kind the tool needs: an operator's review for Jira, the urgent-handoff rule
   of `handoff_prep` for the Slack alert (ai-pipeline §3). The operator must exist; the alert
   needs the ticket's urgency `high`, taken from the run state (the scope), never from arguments.
"""

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

# Re-exported: the nodes are a column of `tool_calls` now (P7-04), so the enum lives in domain.
from hub.domain.enums import ToolNode as ToolNode
from hub.domain.enums import Urgency
from hub.tools.registry import (
    IDEMPOTENCY_KEY,
    IDENTITY_ARGUMENTS,
    MODEL_TOOLS,
    SEARCH_KB,
    TOOLS,
    Identity,
    ToolSpec,
)

CUSTOMER_REF = re.compile(r"^CUS-\d{4}$")
# A denied name is stored for the operator; a model can make it any length.
MAX_NAME_LENGTH = 64
# Stored instead of a tool name that is not in the registry: a model chooses that name, and it
# may hold card data or another customer's reference.
UNKNOWN_TOOL_NAME = "unknown"


class Caller(StrEnum):
    MODEL = "model"  # the research loop passes a model's tool call on unchanged
    CODE = "code"


ALLOWLIST: Mapping[ToolNode, frozenset[str]] = {
    # P7-04: intake loads the customer profile through the tool.
    ToolNode.INTAKE: frozenset({"core_get_customer"}),
    ToolNode.RESEARCH: frozenset(
        {
            SEARCH_KB,
            "core_get_customer",
            "core_list_transactions",
            "core_get_transaction",
            "core_get_cards",
        }
    ),
    ToolNode.HANDOFF_PREP: frozenset({"slack_post_escalation"}),
    ToolNode.ACT: frozenset({"jira_create_issue"}),
    # The operator's account view (P10-03 review): the three read tools, never a side effect.
    ToolNode.CONSOLE: frozenset({"core_get_customer", "core_list_transactions", "core_get_cards"}),
}

MAX_CALLS_PER_RUN: Mapping[ToolNode, int] = {
    ToolNode.INTAKE: 1,
    ToolNode.RESEARCH: 5,
    ToolNode.HANDOFF_PREP: 1,
    ToolNode.ACT: 1,
    # Three calls per view of the Customer tab: ten views of one run, then `run_limit` (the tab
    # shows the data as unknown). The console keeps the answer for the page's lifetime.
    ToolNode.CONSOLE: 30,
}
MAX_SIDE_EFFECTS_PER_TICKET = 1


class ApprovalBasis(StrEnum):
    OPERATOR_REVIEW = "operator_review"
    URGENT_HANDOFF = "urgent_handoff"


REQUIRED_BASIS: Mapping[str, ApprovalBasis] = {
    "jira_create_issue": ApprovalBasis.OPERATOR_REVIEW,
    "slack_post_escalation": ApprovalBasis.URGENT_HANDOFF,
}


class Approval(BaseModel):
    """Permission to run one side-effect tool once. Built by code from an operator's review
    decision (Jira) or from the urgent-handoff rule (Slack); never from model output alone."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    ticket_id: uuid.UUID
    basis: ApprovalBasis
    operator_id: uuid.UUID | None = None
    # The exact arguments approved, without identity arguments.
    arguments: dict[str, Any]


class DenyReason(StrEnum):
    UNKNOWN_TOOL = "unknown_tool"
    NOT_ALLOWED = "not_allowed"
    RUN_LIMIT = "run_limit"
    TICKET_LIMIT = "ticket_limit"
    IDENTITY_ARGUMENT = "identity_argument"
    INVALID_ARGUMENTS = "invalid_arguments"
    INVALID_IDENTITY = "invalid_identity"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_MISMATCH = "approval_mismatch"
    UNKNOWN_OPERATOR = "unknown_operator"
    NOT_URGENT = "not_urgent"


@dataclass(frozen=True)
class ToolScope:
    """Who a call is for and where it comes from. Built by the graph from the tickets row
    (`customer_ref` is the `customers.external_ref` of the ticket's customer), never from model
    output."""

    run_id: uuid.UUID
    ticket_id: uuid.UUID
    customer_ref: str
    node: ToolNode
    # The triage urgency of the ticket (state); the Slack alert needs `high`.
    urgency: Urgency | None = None


@dataclass(frozen=True)
class ToolRequest:
    name: str
    arguments: Mapping[str, Any]
    caller: Caller


@dataclass(frozen=True)
class Allowed:
    spec: ToolSpec
    # What goes to the MCP server: the validated arguments plus the injected identity.
    arguments: dict[str, Any]
    approved_by: uuid.UUID | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True)
class Denied:
    tool: str  # the MCP name when known, else the requested name (shortened)
    reason: DenyReason
    # Names of the arguments concerned (never their values).
    fields: tuple[str, ...] = field(default=())


def idempotency_key(tool: str, ticket_id: uuid.UUID) -> str:
    """One key per tool and ticket: a retry replays the first call, and the server's limit of
    one issue and one alert per ticket matches it."""
    return f"{tool}:{ticket_id}"


def resolve(request: ToolRequest) -> ToolSpec | None:
    if request.caller is Caller.MODEL:
        return MODEL_TOOLS.get(request.name)
    return TOOLS.get(request.name)


def shown_name(name: str) -> str:
    return name[:MAX_NAME_LENGTH]


def authorize(
    request: ToolRequest,
    scope: ToolScope,
    *,
    calls_in_run: int,
    side_effects_for_ticket: int = 0,
    approval: Approval | None = None,
    operator_exists: bool = False,
    ticket_customer_ref: str | None = None,
) -> Allowed | Denied:
    """Decide one tool call. `calls_in_run` counts this node's earlier calls in the run (denied
    ones too); `side_effects_for_ticket` the successful calls of this tool for the ticket;
    `operator_exists` whether the approval's operator is a row in `operators`;
    `ticket_customer_ref` the `external_ref` of the scope ticket's customer (None: not found)."""
    spec = resolve(request)
    if spec is None:
        return Denied(UNKNOWN_TOOL_NAME, DenyReason.UNKNOWN_TOOL)
    if spec.name not in ALLOWLIST[scope.node]:
        return Denied(spec.name, DenyReason.NOT_ALLOWED)
    if calls_in_run >= MAX_CALLS_PER_RUN[scope.node]:
        return Denied(spec.name, DenyReason.RUN_LIMIT)
    if spec.side_effect and side_effects_for_ticket >= MAX_SIDE_EFFECTS_PER_TICKET:
        return Denied(spec.name, DenyReason.TICKET_LIMIT)
    forbidden = sorted(IDENTITY_ARGUMENTS.union({IDEMPOTENCY_KEY}).intersection(request.arguments))
    if forbidden:
        return Denied(spec.name, DenyReason.IDENTITY_ARGUMENT, tuple(forbidden))
    try:
        validated = spec.arguments.model_validate(dict(request.arguments))
    except ValidationError as error:
        names = sorted({str(item["loc"][0]) for item in error.errors() if item["loc"]})
        return Denied(spec.name, DenyReason.INVALID_ARGUMENTS, tuple(shown_name(n) for n in names))
    chosen = validated.model_dump(mode="json", exclude_none=True)
    if Identity.CUSTOMER_REF in spec.injects and not (
        CUSTOMER_REF.fullmatch(scope.customer_ref) and scope.customer_ref == ticket_customer_ref
    ):
        return Denied(spec.name, DenyReason.INVALID_IDENTITY, (Identity.CUSTOMER_REF.value,))
    approved_by: uuid.UUID | None = None
    key: str | None = None
    if spec.side_effect:
        if approval is None:
            return Denied(spec.name, DenyReason.APPROVAL_REQUIRED)
        if not _approval_matches(approval, spec, scope, chosen):
            return Denied(spec.name, DenyReason.APPROVAL_MISMATCH)
        if approval.basis is ApprovalBasis.OPERATOR_REVIEW and not operator_exists:
            return Denied(spec.name, DenyReason.UNKNOWN_OPERATOR)
        if approval.basis is ApprovalBasis.URGENT_HANDOFF and scope.urgency is not Urgency.HIGH:
            return Denied(spec.name, DenyReason.NOT_URGENT, (Identity.URGENCY.value,))
        approved_by = approval.operator_id
        key = idempotency_key(spec.name, scope.ticket_id)
    arguments = {**chosen, **_identity(spec, scope)}
    if key is not None:
        arguments[IDEMPOTENCY_KEY] = key
    return Allowed(spec, arguments, approved_by=approved_by, idempotency_key=key)


def _identity(spec: ToolSpec, scope: ToolScope) -> dict[str, str]:
    values = {
        Identity.CUSTOMER_REF: scope.customer_ref,
        Identity.TICKET_ID: str(scope.ticket_id),
        Identity.URGENCY: str(scope.urgency),
    }
    return {item.value: values[item] for item in spec.injects}


def _approval_matches(
    approval: Approval, spec: ToolSpec, scope: ToolScope, chosen: dict[str, Any]
) -> bool:
    basis = REQUIRED_BASIS[spec.name]
    try:
        approved = spec.arguments.model_validate(approval.arguments)
    except ValidationError:
        return False
    return (
        approval.tool == spec.name
        and approval.ticket_id == scope.ticket_id
        and approval.basis is basis
        and (basis is not ApprovalBasis.OPERATOR_REVIEW or approval.operator_id is not None)
        and approved.model_dump(mode="json", exclude_none=True) == chosen
    )
