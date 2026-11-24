"""The MCP server through real MCP clients: in process, over stdio, and over streamable HTTP."""

import logging
import os
import socket
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
import uvicorn
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer
from mcp.shared._httpx_utils import create_mcp_http_client
from pydantic import BaseModel
from starlette.types import Receive, Scope, Send

from support_tools import smoke
from support_tools.adapters.core import (
    CardList,
    CustomerProfile,
    TransactionDetail,
    TransactionList,
)
from support_tools.adapters.jira import JiraIssue, JiraIssueStatus
from support_tools.adapters.mock_store import MockStore
from support_tools.adapters.slack import SlackMessage
from support_tools.seed import load_seed
from support_tools.server import (
    DEFAULT_MOCK_DB,
    DEFAULT_SEED_DIR,
    BearerAuth,
    ServerSettings,
    _safe,
    http_app,
)

from .conftest import ANCHOR

TICKET = "0b3f6c1e-2a4d-4c8e-9f10-1a2b3c4d5e6f"

TOOLS: dict[str, type[BaseModel]] = {
    "core_get_customer": CustomerProfile,
    "core_list_transactions": TransactionList,
    "core_get_transaction": TransactionDetail,
    "core_get_cards": CardList,
    "jira_create_issue": JiraIssue,
    "jira_get_issue": JiraIssueStatus,
    "slack_post_escalation": SlackMessage,
}

CALLS: dict[str, dict[str, Any]] = {
    "core_get_customer": {"customer_ref": "CUS-0001"},
    "core_list_transactions": {"customer_ref": "CUS-0001", "status": "pending", "limit": 5},
    "core_get_transaction": {"customer_ref": "CUS-0001", "reference": "TX-000900"},
    "core_get_cards": {"customer_ref": "CUS-0001"},
    "jira_create_issue": {
        "issue_type": "dispute",
        "summary": "Disputed card payment",
        "description": "Customer disputes TX-000008.",
        "customer_ref": "CUS-0001",
        "ticket_id": TICKET,
        "idempotency_key": "ticket-1-action-a1",
    },
    "jira_get_issue": {"key": "TB-1"},
    "slack_post_escalation": {
        "ticket_id": TICKET,
        "urgency": "high",
        "text": "Urgent handoff",
        "idempotency_key": "ticket-1-slack",
    },
}


async def test_the_seven_tools_are_listed_with_input_and_output_schemas(server: MCPServer) -> None:
    async with Client(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert set(tools) == set(TOOLS)
    for name, model in TOOLS.items():
        schema = tools[name].output_schema
        assert schema is not None
        assert set(schema["properties"]) == set(model.model_fields)
    assert tools["core_get_customer"].input_schema["properties"]["customer_ref"]["pattern"]
    assert tools["core_list_transactions"].input_schema["properties"]["limit"]["maximum"] == 20
    assert tools["core_get_customer"].annotations is not None
    assert tools["core_get_customer"].annotations.read_only_hint is True
    assert tools["jira_create_issue"].annotations is not None
    assert tools["jira_create_issue"].annotations.read_only_hint is False


async def test_every_tool_returns_structured_content_that_matches_its_schema(
    server: MCPServer,
) -> None:
    async with Client(server) as client:
        for name, arguments in CALLS.items():  # in order: the issue exists before it is read
            result = await client.call_tool(name, arguments)
            assert not result.is_error, (name, result.content)
            assert result.structured_content is not None
            TOOLS[name].model_validate(result.structured_content)


async def test_the_restriction_reason_never_leaves_the_server(server: MCPServer) -> None:
    seed = load_seed(DEFAULT_SEED_DIR)
    restricted = [c for c in seed.customers if c.restricted]
    async with Client(server) as client:
        for customer in restricted:
            result = await client.call_tool(
                "core_get_customer", {"customer_ref": customer.external_ref}
            )
            assert result.structured_content is not None
            assert result.structured_content["restricted"] is True
            text = str(result.structured_content) + str(result.content)
            assert customer.restriction_reason is not None
            assert customer.restriction_reason not in text
            assert "reason" not in text
            assert "support_scenarios" not in text


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("core_get_customer", {"customer_ref": "CUS-1"}),
        ("core_get_customer", {"customer_ref": "CUS-0001' OR 1=1"}),
        ("core_get_customer", {}),
        ("core_list_transactions", {"customer_ref": "CUS-0001", "limit": 21}),
        ("core_list_transactions", {"customer_ref": "CUS-0001", "type": "crypto"}),
        ("core_get_transaction", {"customer_ref": "CUS-0001", "reference": "TX-1"}),
        ("jira_create_issue", {**CALLS["jira_create_issue"], "issue_type": "refund"}),
        ("jira_create_issue", {**CALLS["jira_create_issue"], "ticket_id": "T-1"}),
        ("jira_create_issue", {**CALLS["jira_create_issue"], "idempotency_key": "short"}),
        ("jira_create_issue", {**CALLS["jira_create_issue"], "summary": "x" * 256}),
        ("jira_get_issue", {"key": "TB-0"}),
        ("slack_post_escalation", {**CALLS["slack_post_escalation"], "urgency": "panic"}),
        ("slack_post_escalation", {**CALLS["slack_post_escalation"], "text": ""}),
    ],
)
async def test_invalid_arguments_are_a_tool_error(
    server: MCPServer, name: str, arguments: dict[str, Any]
) -> None:
    async with Client(server) as client:
        result = await client.call_tool(name, arguments)
    assert result.is_error
    assert result.structured_content is None


@pytest.mark.parametrize(
    ("name", "arguments", "message"),
    [
        ("core_get_customer", {"customer_ref": "CUS-0999"}, "customer not found"),
        ("core_get_transaction", {"customer_ref": "CUS-0001", "reference": "TX-000012"},
         "transaction not found"),
        ("jira_get_issue", {"key": "TB-7"}, "issue not found"),
    ],
)  # fmt: skip
async def test_an_anticipated_failure_is_a_short_safe_tool_error(
    server: MCPServer, name: str, arguments: dict[str, Any], message: str
) -> None:
    async with Client(server) as client:
        result = await client.call_tool(name, arguments)
    assert result.is_error
    assert result.content[0].type == "text"
    assert result.content[0].text.endswith(message)


async def test_the_same_idempotency_key_twice_is_one_issue_through_mcp(server: MCPServer) -> None:
    async with Client(server) as client:
        first = await client.call_tool("jira_create_issue", CALLS["jira_create_issue"])
        second = await client.call_tool("jira_create_issue", CALLS["jira_create_issue"])
        third = await client.call_tool(
            "jira_create_issue", {**CALLS["jira_create_issue"], "idempotency_key": "ticket-1-other"}
        )
    assert first.structured_content is not None
    assert second.structured_content is not None
    assert first.structured_content["key"] == second.structured_content["key"] == "TB-1"
    assert [first.structured_content["created"], second.structured_content["created"]] == [
        True,
        False,
    ]
    assert third.is_error


def test_settings_come_from_the_environment() -> None:
    settings = ServerSettings.from_env(
        {
            "CORE_ANCHOR_DATE": "2026-11-12",
            "MCP_SERVER_TOKEN": "abc",
            "SEED_DIR": "/seed",
            "MCP_MOCK_DB": "/data/m.sqlite",
        }
    )
    assert settings == ServerSettings(Path("/seed"), Path("/data/m.sqlite"), ANCHOR, "abc")
    pasted = ServerSettings.from_env({"MCP_SERVER_TOKEN": "abc\n"})
    # A pasted secret's trailing newline is dropped.
    assert pasted.token == "abc"  # noqa: S105 - a test value
    defaults = ServerSettings.from_env({"CORE_ANCHOR_DATE": " "})
    assert defaults == ServerSettings(DEFAULT_SEED_DIR, DEFAULT_MOCK_DB, None, "")


async def test_an_empty_anchor_uses_today_in_cet(tmp_path: Path) -> None:
    from support_tools.server import build_server, today_in_cet

    settings = ServerSettings(DEFAULT_SEED_DIR, tmp_path / "m.sqlite", None, "")
    store = MockStore(settings.mock_db)
    async with Client(build_server(settings, store=store)) as client:
        result = await client.call_tool("core_get_cards", {"customer_ref": "CUS-0001"})
    store.close()
    assert result.structured_content is not None
    assert result.structured_content["as_of"] == today_in_cet().isoformat()


def test_the_http_transport_needs_a_token() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        raise AssertionError

    with pytest.raises(ValueError, match="MCP_SERVER_TOKEN"):
        BearerAuth(app, "")


# --- over the wire ----------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@pytest.fixture
def http_url(server: MCPServer, settings: ServerSettings) -> Iterator[str]:
    port = _free_port()
    config = uvicorn.Config(
        http_app(server, settings.token, "127.0.0.1"),
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )
    running = uvicorn.Server(config)
    thread = threading.Thread(target=running.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not running.started:
        if time.monotonic() > deadline:
            raise RuntimeError("the test server did not start")
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}/mcp"
    running.should_exit = True
    thread.join(timeout=10)


@pytest.mark.parametrize("header", [None, "Bearer wrong", "Basic abc"])
async def test_a_request_without_the_right_bearer_token_is_refused(
    http_url: str, header: str | None
) -> None:
    headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
    if header is not None:
        headers["authorization"] = header
    async with httpx2.AsyncClient() as http:
        response = await http.post(
            http_url, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "ping"}
        )
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_tools_can_be_listed_and_called_over_streamable_http(
    http_url: str, settings: ServerSettings
) -> None:
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {settings.token}"})
    async with http, Client(streamable_http_client(http_url, http_client=http)) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        result = await client.call_tool("core_get_customer", {"customer_ref": "CUS-0001"})
    assert names == set(TOOLS)
    assert result.structured_content is not None
    assert result.structured_content["customer_ref"] == "CUS-0001"


async def test_tools_can_be_called_over_stdio(tmp_path: Path) -> None:
    """The transport Claude Code uses: the server as a subprocess."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "support_tools", "--transport", "stdio"],
        env={
            "PATH": os.environ.get("PATH", ""),
            "CORE_ANCHOR_DATE": ANCHOR.isoformat(),
            "MCP_MOCK_DB": str(tmp_path / "stdio.sqlite"),
        },
    )
    async with Client(params) as client:
        result = await client.call_tool(
            "core_get_transaction", {"customer_ref": "CUS-0001", "reference": "TX-000900"}
        )
    assert result.structured_content is not None
    assert result.structured_content["reference"] == "TX-000900"
    assert result.structured_content["as_of"] == ANCHOR.isoformat()


# --- error text never repeats an argument (PR #39 review) -------------------------------------

# A public test card number (Visa test range); never a real one (AGENTS.md).
TEST_CARD = "4111111111111111"


@pytest.mark.parametrize(
    ("name", "arguments", "fields"),
    [
        ("core_get_customer", {"customer_ref": TEST_CARD}, "customer_ref"),
        (
            "jira_create_issue",
            {**CALLS["jira_create_issue"], "summary": f"card {TEST_CARD} " * 20},
            "summary",
        ),
        (
            "slack_post_escalation",
            {**CALLS["slack_post_escalation"], "idempotency_key": f"{TEST_CARD} x"},
            "idempotency_key",
        ),
        ("core_list_transactions", {"customer_ref": "CUS-0001", "status": TEST_CARD}, "status"),
    ],
)
async def test_an_invalid_argument_error_names_the_field_never_the_value(
    server: MCPServer,
    name: str,
    arguments: dict[str, Any],
    fields: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    async with Client(server) as client:
        result = await client.call_tool(name, arguments)
    assert result.is_error
    assert result.content[0].type == "text"
    text = result.content[0].text
    assert text == f"Error executing tool {name}: invalid arguments: {fields}"
    assert TEST_CARD not in text
    assert TEST_CARD not in caplog.text


def test_safe_lets_other_errors_through_to_the_generic_sdk_message() -> None:
    def broken() -> None:
        raise ValueError(f"value {TEST_CARD}")

    with pytest.raises(ValueError, match="value"):
        _safe(broken)


async def test_an_unexpected_failure_in_a_tool_is_generic(server: MCPServer) -> None:
    @server.tool()
    def leaky() -> str:
        raise ValueError(f"value {TEST_CARD}")

    async with Client(server) as client:
        result = await client.call_tool("leaky", {})
    assert result.is_error
    assert result.content[0].type == "text"
    assert result.content[0].text == "Error executing tool leaky"


async def test_a_jira_issue_needs_a_known_customer(server: MCPServer) -> None:
    async with Client(server) as client:
        result = await client.call_tool(
            "jira_create_issue", {**CALLS["jira_create_issue"], "customer_ref": "CUS-0999"}
        )
    assert result.is_error
    assert result.content[0].type == "text"
    assert result.content[0].text.endswith("customer not found")


# --- bearer check -----------------------------------------------------------------------------


async def _call_auth(scope_type: str, header: bytes | None) -> list[dict[str, Any]]:
    reached: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        reached.append(scope["type"])

    sent: list[dict[str, Any]] = []

    async def send(message: Any) -> None:  # noqa: ANN401 - ASGI message
        sent.append(message)

    async def receive() -> Any:  # noqa: ANN401 - ASGI message
        return {}

    headers = [] if header is None else [(b"authorization", header)]
    await BearerAuth(app, "secret-token")({"type": scope_type, "headers": headers}, receive, send)
    if reached:
        sent.append({"type": "reached"})
    return sent


@pytest.mark.parametrize(
    "header", [b"Bearer secret-token", b"bearer secret-token", b"BEARER secret-token"]
)
async def test_the_bearer_scheme_is_case_insensitive(header: bytes) -> None:
    assert await _call_auth("http", header) == [{"type": "reached"}]


@pytest.mark.parametrize(
    "header", [None, b"Bearer", b"Bearer wrong", b"Basic secret-token", b"secret-token"]
)
async def test_a_wrong_or_missing_token_is_401(header: bytes | None) -> None:
    sent = await _call_auth("http", header)
    assert sent[0]["status"] == 401


async def test_a_websocket_is_closed_and_lifespan_passes() -> None:
    assert await _call_auth("websocket", b"Bearer secret-token") == [
        {"type": "websocket.close", "code": 1008}
    ]
    assert await _call_auth("lifespan", None) == [{"type": "reached"}]


def test_the_smoke_script_passes(capsys: pytest.CaptureFixture[str]) -> None:
    """`make mcp-smoke` keeps working: both transports, real subprocesses."""
    smoke.main()
    out = capsys.readouterr().out
    assert "[http] request without a token: HTTP 401" in out
    assert out.strip().endswith("mcp smoke: ok")
