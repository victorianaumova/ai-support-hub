"""The `support-tools` MCP server (API §7, ADR-0011).

Streamable HTTP at `/mcp` for the Hub, behind a bearer token (`MCP_SERVER_TOKEN`); stdio for
Claude Code. Every tool validates its arguments against its input schema (the reference formats
of the seed, security T4) and returns structured content that matches its output schema. An
anticipated failure is a tool error with a short safe message; any other exception reaches the
client only as the SDK's generic "Error executing tool <name>".
"""

import argparse
import hmac
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import CallToolResult, InputRequiredResult, ToolAnnotations
from pydantic import Field, ValidationError
from starlette.types import ASGIApp, Receive, Scope, Send

from support_tools.adapters.core import (
    MAX_TRANSACTIONS,
    CardList,
    CoreAdapter,
    CoreRequestError,
    CustomerProfile,
    TransactionDetail,
    TransactionList,
)
from support_tools.adapters.jira import IssueType, JiraIssue, JiraIssueStatus, JiraMock
from support_tools.adapters.mock_store import MockRefusedError, MockStore
from support_tools.adapters.slack import SlackMessage, SlackMock, Urgency
from support_tools.business_days import CET
from support_tools.seed import (
    CUSTOMER_REF_PATTERN,
    TRANSACTION_REF_PATTERN,
    TransactionStatus,
    TransactionType,
    load_seed,
)

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_SEED_DIR = REPO_ROOT / "data" / "seed"
DEFAULT_MOCK_DB = REPO_ROOT / ".cache" / "mcp-mock.sqlite"
DEFAULT_PORT = 8001
MCP_PATH = "/mcp"

CustomerRefArg = Annotated[
    str, Field(pattern=CUSTOMER_REF_PATTERN, description="Customer reference, e.g. CUS-0001")
]
TransactionRefArg = Annotated[
    str, Field(pattern=TRANSACTION_REF_PATTERN, description="Transaction reference, e.g. TX-000001")
]
TicketIdArg = Annotated[
    str,
    Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        description="Hub ticket id (UUID, lower case)",
    ),
]
IdempotencyKeyArg = Annotated[
    str,
    Field(
        min_length=8,
        max_length=200,
        pattern=r"^[A-Za-z0-9._:-]+$",
        description="The same key with the same arguments returns the first result",
    ),
]
JiraKeyArg = Annotated[str, Field(pattern=r"^TB-[1-9]\d{0,8}$", description="Issue key, e.g. TB-1")]

_READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
_WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


@dataclass(frozen=True)
class ServerSettings:
    """From the environment (operations §2); the server has no `.env` loader of its own."""

    seed_dir: Path
    mock_db: Path
    anchor_date: date | None
    token: str

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "ServerSettings":
        env = os.environ if environ is None else environ
        anchor = env.get("CORE_ANCHOR_DATE", "").strip()
        return cls(
            seed_dir=Path(env.get("SEED_DIR") or DEFAULT_SEED_DIR),
            mock_db=Path(env.get("MCP_MOCK_DB") or DEFAULT_MOCK_DB),
            anchor_date=date.fromisoformat(anchor) if anchor else None,
            # A pasted secret may end in a newline (P10-07); the Hub strips it the same way.
            token=env.get("MCP_SERVER_TOKEN", "").strip(),
        )


def today_in_cet() -> date:
    return datetime.now(CET).date()


def build_server(
    settings: ServerSettings,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    store: MockStore | None = None,
) -> MCPServer:
    """The server with its seven tools. `now` is the mocks' clock and `store` the mocks' table
    (tests pass their own and close it); by default the store lives as long as the process."""
    anchor = settings.anchor_date
    core = CoreAdapter(
        load_seed(settings.seed_dir),
        today=(lambda: anchor) if anchor is not None else today_in_cet,
    )
    store = store or MockStore(settings.mock_db)
    jira = JiraMock(store, now)
    slack = SlackMock(store, now)
    server = SupportToolsServer(
        "support-tools",
        instructions=(
            "Tallybridge support tools (fictional company, mock data). Core tools read one "
            "customer's account data; Jira and Slack tools are mocks that store the call."
        ),
    )

    @server.tool(annotations=_READ)
    def core_get_customer(customer_ref: CustomerRefArg) -> CustomerProfile:
        """Tier, country, verification status, restriction flag (never a reason), limits and
        how much of each is used, per currency. Daily limits count only transactions dated
        today (CET) and monthly limits this calendar month; an empty `used` means no such
        transaction is recorded in that period, not that a payment cannot be pending elsewhere."""
        return _safe(lambda: core.get_customer(customer_ref))

    @server.tool(annotations=_READ)
    def core_list_transactions(
        customer_ref: CustomerRefArg,
        type: TransactionType | None = None,
        status: TransactionStatus | None = None,
        limit: Annotated[int, Field(ge=1, le=MAX_TRANSACTIONS)] = 10,
    ) -> TransactionList:
        """The customer's newest transactions, optionally filtered by type and status."""
        return _safe(lambda: core.list_transactions(customer_ref, type, status, limit))

    @server.tool(annotations=_READ)
    def core_get_transaction(
        customer_ref: CustomerRefArg, reference: TransactionRefArg
    ) -> TransactionDetail:
        """One transaction of the customer with its status history and expected date."""
        return _safe(lambda: core.get_transaction(customer_ref, reference))

    @server.tool(annotations=_READ)
    def core_get_cards(customer_ref: CustomerRefArg) -> CardList:
        """The customer's cards: last 4 digits, kind, status, delivery status."""
        return _safe(lambda: core.get_cards(customer_ref))

    @server.tool(annotations=_WRITE)
    def jira_create_issue(
        issue_type: IssueType,
        summary: Annotated[str, Field(min_length=1, max_length=255)],
        description: Annotated[str, Field(min_length=1, max_length=5000)],
        customer_ref: CustomerRefArg,
        ticket_id: TicketIdArg,
        idempotency_key: IdempotencyKeyArg,
    ) -> JiraIssue:
        """Create a Jira issue (mock) for a known customer. At most one issue per ticket."""

        def create() -> JiraIssue:
            core.require_customer(customer_ref)
            return jira.create_issue(
                issue_type, summary, description, customer_ref, ticket_id, idempotency_key
            )

        return _safe(create)

    @server.tool(annotations=_READ)
    def jira_get_issue(key: JiraKeyArg) -> JiraIssueStatus:
        """Status and assignee of a Jira issue (mock)."""
        return _safe(lambda: jira.get_issue(key))

    @server.tool(annotations=_WRITE)
    def slack_post_escalation(
        ticket_id: TicketIdArg,
        urgency: Urgency,
        text: Annotated[str, Field(min_length=1, max_length=3000)],
        idempotency_key: IdempotencyKeyArg,
    ) -> SlackMessage:
        """Post an escalation alert to the support channel (mock). At most one per ticket."""
        return _safe(lambda: slack.post_escalation(ticket_id, urgency, text, idempotency_key))

    return server


def _safe[T](call: Callable[[], T]) -> T:
    """Anticipated failures become tool errors with their own fixed, data-free message.

    Only the adapters' own errors are caught: their messages are constants. Anything else,
    including a pydantic `ValidationError` (a `ValueError` whose text holds the values), is left
    to the SDK, which answers only "Error executing tool <name>".
    """
    try:
        return call()
    except (CoreRequestError, MockRefusedError) as exc:
        raise ToolError(str(exc)) from exc


class SupportToolsServer(MCPServer):
    """`MCPServer` whose argument errors name the fields, never the values.

    The SDK reports arguments that fail the input schema with pydantic's text, which repeats the
    rejected value (`input_value=...`). A caller may have put card data or customer text there,
    and a tool error goes back to the model and into logs, so only the field names are kept.
    """

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Context[Any, Any] | None = None
    ) -> CallToolResult | InputRequiredResult:
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as exc:
            cause = exc.__cause__
            if isinstance(exc, UnexpectedToolError) or not isinstance(cause, ValidationError):
                raise
            fields = sorted(
                {".".join(str(p) for p in e["loc"]) or "arguments" for e in cause.errors()}
            )
            raise ToolError(
                f"Error executing tool {name}: invalid arguments: {', '.join(fields)}"
            ) from cause


class BearerAuth:
    """Pure ASGI check of `Authorization: Bearer <MCP_SERVER_TOKEN>` on every request.

    The scheme is case-insensitive (RFC 9110 §11.1), the token is compared in constant time.
    Lifespan events pass through, so the session manager starts; a websocket (no route uses one)
    is closed. In production Cloud Run's own authentication is the first check (security §4).
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        if not token:
            raise ValueError("MCP_SERVER_TOKEN is empty: the HTTP transport needs a token")
        self._app = app
        self._token = token.encode()

    def _authorized(self, scope: Scope) -> bool:
        header: bytes = dict(scope["headers"]).get(b"authorization", b"")
        scheme, _, given = header.partition(b" ")
        # compare_digest always runs, so a wrong scheme costs the same time as a wrong token.
        same_token = hmac.compare_digest(given.strip(), self._token)
        return scheme.lower() == b"bearer" and same_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] == "http" and not self._authorized(scope):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
            return
        await self._app(scope, receive, send)


def http_app(server: MCPServer, token: str, host: str) -> ASGIApp:
    return BearerAuth(server.streamable_http_app(streamable_http_path=MCP_PATH, host=host), token)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m support_tools")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    settings = ServerSettings.from_env()
    server = build_server(settings)
    if args.transport == "stdio":
        server.run("stdio")
        return
    import uvicorn

    uvicorn.run(http_app(server, settings.token, args.host), host=args.host, port=args.port)
