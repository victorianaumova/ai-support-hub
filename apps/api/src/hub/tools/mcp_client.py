"""The Hub's client of the `support-tools` MCP server (API §7, ADR-0011).

Nodes never call MCP directly. They open a `ToolSession` for their scope (run, ticket, customer,
node) and call tools through it; every call is decided by `hub.tools.permissions` first and
recorded in `tool_calls` with an audit record (`tool.called` or `tool.denied`), allowed or not.

The transport opens one MCP session per call over streamable HTTP with the bearer token. A tool
error, a timeout or a broken connection is an `error` outcome with a short safe text; nothing
from here raises into the node for those.

P7-04:
- The per-run limit is counted from the run's `tool_calls` rows of the scope's node (denied ones
  too), not in memory, so it holds across a resume or a second session of the same run.
- A tool that injects `customer_ref` runs only when the scope's reference is the one of the
  scope ticket's own customer in the database (`invalid_identity` otherwise).
- A local tool (`search_kb`) is decided and recorded like the others; the session's caller
  passes its handler, which returns the value for the caller and a short summary to store.

Every call, denied ones too, is also a tool observation in the run's trace (P9-01): the stored
(masked) arguments and the structured result, which the tracer filters (G9: card data masked,
any restriction-reason key dropped). Outside a traced run this does nothing.
"""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import func, select

from hub.audit import AuditValueError
from hub.audit import record as audit_record
from hub.audit.chain import mask_details
from hub.db.models import Customer, Operator, Run, Ticket, ToolCall
from hub.db.session import Database
from hub.domain.enums import ActorType, ToolCallStatus, ToolNode
from hub.guardrails.masking import mask_text
from hub.observability.tracing import Tracer
from hub.tools.permissions import (
    MAX_NAME_LENGTH,
    Allowed,
    Approval,
    Caller,
    Denied,
    DenyReason,
    ToolRequest,
    ToolScope,
    authorize,
    resolve,
)
from hub.tools.registry import Identity

logger = logging.getLogger(__name__)

CALLED_ACTION = "tool.called"
DENIED_ACTION = "tool.denied"
DEFAULT_TIMEOUT_S = 10.0
MAX_SUMMARY_LENGTH = 300
MAX_ARGUMENTS_JSON = 2000
# Result fields that name the created object (`tool_calls.external_ref`).
EXTERNAL_REF_FIELDS = ("key", "ts")


@dataclass(frozen=True)
class TransportResult:
    is_error: bool
    structured: dict[str, Any] | None
    error_text: str = ""


class ToolTransport(Protocol):
    async def call(
        self, name: str, arguments: dict[str, Any], timeout_s: float
    ) -> TransportResult: ...


class McpTransport:
    """Streamable HTTP to the server at `url` with `Authorization: Bearer <token>`."""

    def __init__(self, url: str, token: str) -> None:
        self._url = url
        self._token = token

    async def call(self, name: str, arguments: dict[str, Any], timeout_s: float) -> TransportResult:
        headers = {"Authorization": f"Bearer {self._token}"}
        http = httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(timeout_s))
        transport = streamable_http_client(self._url, http_client=http)
        async with http, Client(transport, read_timeout_seconds=timeout_s) as client:
            result = await client.call_tool(name, arguments)
        text = " ".join(getattr(item, "text", "") for item in result.content)
        return TransportResult(result.is_error, result.structured_content, text)


@dataclass(frozen=True)
class LocalResult:
    """What a local tool's handler returns: `result` goes to the caller only; `summary` is
    stored (masked, shortened), so it must hold no text the table may not keep."""

    result: dict[str, Any]
    summary: str


# A local tool's handler gets the validated arguments.
type LocalTool = Callable[[dict[str, Any]], Awaitable[LocalResult]]


@dataclass(frozen=True)
class ToolOutcome:
    tool: str
    status: ToolCallStatus
    # The structured result of an `ok` call; None otherwise. Not masked: it goes to the caller
    # (the research loop wraps it as data); what is stored is masked.
    result: dict[str, Any] | None = None
    # A short safe text for `error` and `denied` (the deny reason, or the server's message).
    error: str | None = None


@dataclass(frozen=True)
class ToolCallRow:
    """What `ToolCallRecorder` stores: one `tool_calls` row and its audit record."""

    scope: ToolScope
    caller: Caller
    tool: str
    status: ToolCallStatus
    arguments: dict[str, Any]
    side_effect: bool
    latency_ms: int
    result_summary: str | None = None
    approved_by: uuid.UUID | None = None
    idempotency_key: str | None = None
    external_ref: str | None = None
    reason: str | None = None
    fields: tuple[str, ...] = ()


class ToolCallRecorder(Protocol):
    async def side_effects_done(self, ticket_id: uuid.UUID, tool: str) -> int:
        """Successful calls of `tool` in any run of the ticket."""

    async def calls_in_run(self, run_id: uuid.UUID, node: ToolNode) -> int:
        """Every recorded call of `node` in the run, denied ones too."""

    async def customer_ref_of(self, ticket_id: uuid.UUID) -> str | None:
        """The `external_ref` of the ticket's customer; None when the ticket does not exist."""

    async def operator_exists(self, operator_id: uuid.UUID) -> bool:
        """Whether an approval's operator is a row in `operators`."""

    async def record(self, row: ToolCallRow) -> None:
        """Store the row and its audit record in one transaction."""


class DbToolCallRecorder:
    """Each row is committed on its own, so a call stays recorded when the node fails later."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def side_effects_done(self, ticket_id: uuid.UUID, tool: str) -> int:
        async with self._database.sessions() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(ToolCall)
                .join(Run, Run.id == ToolCall.run_id)
                .where(
                    Run.ticket_id == ticket_id,
                    ToolCall.tool == tool,
                    ToolCall.status == ToolCallStatus.OK,
                )
            )
        return int(count or 0)

    async def operator_exists(self, operator_id: uuid.UUID) -> bool:
        async with self._database.sessions() as session:
            found = await session.scalar(select(Operator.id).where(Operator.id == operator_id))
        return found is not None

    async def calls_in_run(self, run_id: uuid.UUID, node: ToolNode) -> int:
        async with self._database.sessions() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(ToolCall)
                .where(ToolCall.run_id == run_id, ToolCall.node == node)
            )
        return int(count or 0)

    async def customer_ref_of(self, ticket_id: uuid.UUID) -> str | None:
        async with self._database.sessions() as session:
            return await session.scalar(
                select(Customer.external_ref)
                .join(Ticket, Ticket.customer_id == Customer.id)
                .where(Ticket.id == ticket_id)
            )

    async def record(self, row: ToolCallRow) -> None:
        async with self._database.sessions.begin() as session:
            entry = ToolCall(
                run_id=row.scope.run_id,
                node=row.scope.node,
                tool=row.tool,
                arguments=row.arguments,
                result_summary=row.result_summary,
                status=row.status,
                side_effect=row.side_effect,
                approved_by=row.approved_by,
                idempotency_key=row.idempotency_key,
                external_ref=row.external_ref,
                latency_ms=row.latency_ms,
            )
            session.add(entry)
            await session.flush()
            await audit_record(
                session,
                actor_type=ActorType.AI if row.caller is Caller.MODEL else ActorType.SYSTEM,
                actor_id=None,
                action=DENIED_ACTION if row.status is ToolCallStatus.DENIED else CALLED_ACTION,
                entity_type="run",
                entity_id=row.scope.run_id,
                details=audit_details(row, entry.id),
            )


def audit_details(row: ToolCallRow, tool_call_id: uuid.UUID) -> dict[str, object]:
    """Never argument values or results: the tool, the node, who asked and the outcome."""
    details: dict[str, object] = {
        "tool_call_id": tool_call_id,
        "tool": row.tool,
        "node": row.scope.node.value,
        "caller": row.caller.value,
        "status": row.status.value,
        "side_effect": row.side_effect,
    }
    if row.reason is not None:
        details["reason"] = row.reason
    if row.fields:
        details["fields"] = list(row.fields)
    if row.approved_by is not None:
        details["approved_by"] = row.approved_by
    if row.external_ref is not None:
        details["external_ref"] = row.external_ref
    return details


def stored_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Masked arguments for `tool_calls.arguments`; only the names when the values cannot be
    stored or are too long (a model can send anything)."""
    try:
        masked = mask_details(arguments)
        if len(json.dumps(masked)) <= MAX_ARGUMENTS_JSON:
            return masked
    except (AuditValueError, TypeError):
        pass
    return argument_names(arguments)


def argument_names(arguments: Mapping[str, Any]) -> dict[str, Any]:
    return {"names": sorted(mask_text(str(name))[:MAX_NAME_LENGTH] for name in arguments)}


def result_summary(result: dict[str, Any] | None) -> str | None:
    if result is None:
        return None
    return mask_text(json.dumps(result, sort_keys=True, default=str))[:MAX_SUMMARY_LENGTH]


def external_ref(result: dict[str, Any] | None) -> str | None:
    if result is None:
        return None
    for name in EXTERNAL_REF_FIELDS:
        value = result.get(name)
        if isinstance(value, str):
            return value
    return None


class ToolClient:
    def __init__(
        self,
        transport: ToolTransport,
        recorder: ToolCallRecorder,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        tracer: Tracer | None = None,
    ) -> None:
        self._transport = transport
        self._recorder = recorder
        self._timeout_s = timeout_s
        self._tracer = tracer or Tracer()

    async def calls_in_run(self, scope: ToolScope) -> int:
        return await self._recorder.calls_in_run(scope.run_id, scope.node)

    def session(
        self, scope: ToolScope, local: Mapping[str, LocalTool] | None = None
    ) -> "ToolSession":
        """`local`: the handlers of the local tools this session may run, by tool name."""
        return ToolSession(self, scope, dict(local or {}))

    async def run(
        self,
        request: ToolRequest,
        scope: ToolScope,
        approval: Approval | None,
        local: Mapping[str, LocalTool] | None = None,
        timeout_s: float | None = None,
    ) -> ToolOutcome:
        """`timeout_s`: a shorter limit for this call (never longer than the client's), for a
        caller with a tight budget of its own (the urgent alert, P7-05). A call cut by it is an
        `error` outcome, recorded like any other."""
        decision = await self._decide(request, scope, approval)
        if isinstance(decision, Denied):
            if decision.reason is DenyReason.INVALID_IDENTITY:
                # Not a normal denial: the scope does not match the ticket's customer, which
                # means a bug in how the scope was built. Audited as `tool.denied` below too.
                logger.warning(
                    "tool call with an invalid identity",
                    extra={
                        "run_id": str(scope.run_id),
                        "ticket_id": str(scope.ticket_id),
                        "tool": decision.tool,
                        "node": scope.node.value,
                    },
                )
            self._tracer.start(
                decision.tool,
                as_type="tool",
                input=argument_names(request.arguments),
                metadata={"node": scope.node.value, "caller": request.caller.value},
            ).end(level="WARNING", status_message=f"denied: {decision.reason.value}")
            await self._recorder.record(
                ToolCallRow(
                    scope=scope,
                    caller=request.caller,
                    tool=decision.tool,
                    status=ToolCallStatus.DENIED,
                    # Only names: a denied call may carry another customer's reference.
                    arguments=argument_names(request.arguments),
                    side_effect=False,
                    latency_ms=0,
                    reason=decision.reason.value,
                    fields=decision.fields,
                )
            )
            return ToolOutcome(decision.tool, ToolCallStatus.DENIED, error=decision.reason.value)
        limit = self._timeout_s if timeout_s is None else min(timeout_s, self._timeout_s)
        return await self._execute(decision, request.caller, scope, local or {}, limit)

    async def _decide(
        self, request: ToolRequest, scope: ToolScope, approval: Approval | None
    ) -> Allowed | Denied:
        spec = resolve(request)
        done = 0
        operator_exists = False
        owner: str | None = None
        if spec is not None and spec.side_effect:
            done = await self._recorder.side_effects_done(scope.ticket_id, spec.name)
            if approval is not None and approval.operator_id is not None:
                operator_exists = await self._recorder.operator_exists(approval.operator_id)
        if spec is not None and Identity.CUSTOMER_REF in spec.injects:
            owner = await self._recorder.customer_ref_of(scope.ticket_id)
        return authorize(
            request,
            scope,
            calls_in_run=await self._recorder.calls_in_run(scope.run_id, scope.node),
            side_effects_for_ticket=done,
            approval=approval,
            operator_exists=operator_exists,
            ticket_customer_ref=owner,
        )

    async def _call(
        self, allowed: Allowed, local: Mapping[str, LocalTool], timeout_s: float
    ) -> tuple[TransportResult, str | None]:
        """The answer, and for a local tool its summary to store."""
        name = allowed.spec.name
        if not allowed.spec.local:
            # The transport has its own timeouts; this one holds whatever it does.
            async with asyncio.timeout(timeout_s):
                return await self._transport.call(name, allowed.arguments, timeout_s), None
        handler = local.get(name)
        if handler is None:
            return TransportResult(True, None, "local tool not available"), None
        async with asyncio.timeout(timeout_s):
            done = await handler(allowed.arguments)
        return TransportResult(False, done.result), done.summary

    async def _execute(
        self,
        allowed: Allowed,
        caller: Caller,
        scope: ToolScope,
        local: Mapping[str, LocalTool],
        timeout_s: float,
    ) -> ToolOutcome:
        name = allowed.spec.name
        observation = self._tracer.start(
            name,
            as_type="tool",
            input=stored_arguments(allowed.arguments),
            metadata={
                "node": scope.node.value,
                "caller": caller.value,
                "side_effect": allowed.spec.side_effect,
            },
        )
        started = time.monotonic()
        summary: str | None = None
        try:
            answer, summary = await self._call(allowed, local, timeout_s)
        except Exception as failure:  # any failure is an `error` outcome; only its class is kept
            answer = TransportResult(True, None, type(failure).__name__)
        latency_ms = max(0, round((time.monotonic() - started) * 1000))
        ok = not answer.is_error and answer.structured is not None
        status = ToolCallStatus.OK if ok else ToolCallStatus.ERROR
        error = None if ok else mask_text(answer.error_text or "no structured result")[:200]
        result = answer.structured if ok else None
        stored = (
            mask_text(summary)[:MAX_SUMMARY_LENGTH] if summary is not None else None
        ) or result_summary(result)
        await self._recorder.record(
            ToolCallRow(
                scope=scope,
                caller=caller,
                tool=name,
                status=status,
                arguments=stored_arguments(allowed.arguments),
                side_effect=allowed.spec.side_effect,
                latency_ms=latency_ms,
                result_summary=stored if ok else error,
                approved_by=allowed.approved_by,
                idempotency_key=allowed.idempotency_key if ok else None,
                external_ref=external_ref(result) if allowed.spec.side_effect else None,
            )
        )
        observation.end(
            output=result if ok else None,
            level="DEFAULT" if ok else "ERROR",
            status_message=error,
            metadata={"status": status.value, "latency_ms": latency_ms},
        )
        return ToolOutcome(name, status, result=result, error=error)


class ToolSession:
    """The calls of one node in one run. The per-run limit is counted from the recorded rows,
    so a second session of the same run and node continues the count."""

    def __init__(
        self, client: ToolClient, scope: ToolScope, local: dict[str, LocalTool] | None = None
    ) -> None:
        self._client = client
        self.scope = scope
        self._local = local or {}

    async def call(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        caller: Caller = Caller.CODE,
        approval: Approval | None = None,
        timeout_s: float | None = None,
    ) -> ToolOutcome:
        request = ToolRequest(name=name, arguments=arguments or {}, caller=caller)
        return await self._client.run(request, self.scope, approval, self._local, timeout_s)

    def local(self, name: str, handler: LocalTool) -> None:
        """Give the session the handler of a local tool."""
        self._local[name] = handler

    async def calls_used(self) -> int:
        """This node's recorded calls in the run, denied ones too (the limit's count)."""
        return await self._client.calls_in_run(self.scope)
