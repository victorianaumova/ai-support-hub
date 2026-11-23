"""Checks behind `/ready`: does each dependency answer? A check raises when it does not, and
may return details (numbers only) that `/ready` shows next to its status.

Each check opens and closes its own connection: readiness is probed rarely, and this keeps the
checks free of app lifecycle. The caller applies the timeout.
"""

from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx2
import psycopg

# Details: numbers only (they are shown on a public endpoint).
ReadinessCheck = Callable[[], Awaitable[dict[str, int] | None]]
Connect = Callable[..., Awaitable[Any]]


def libpq_url(sqlalchemy_url: str) -> str:
    """`postgresql+psycopg://…` (SQLAlchemy) → `postgresql://…` (psycopg)."""
    scheme, sep, rest = sqlalchemy_url.partition("://")
    return f"{scheme.split('+', 1)[0]}{sep}{rest}"


def postgres_check(
    database_url: str, connect: Connect = psycopg.AsyncConnection.connect
) -> ReadinessCheck:
    async def check() -> None:
        connection = await connect(libpq_url(database_url), connect_timeout=2)
        async with connection:
            await connection.execute("SELECT 1")
        return None

    return check


class UnexpectedStatusError(Exception):
    """The dependency answered, but not with a status that shows it is usable."""


def http_check(
    url: str,
    *,
    accept: Callable[[int], bool],
    headers: dict[str, str] | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> ReadinessCheck:
    """GET `url`; fails unless `accept(status)` is true. Redirects are not followed."""

    async def check() -> None:
        async with httpx2.AsyncClient(transport=transport, headers=headers) as client:
            response = await client.get(url)
        if not accept(response.status_code):
            raise UnexpectedStatusError(f"HTTP {response.status_code}")
        return None

    return check


def _is_success(status: int) -> bool:
    return 200 <= status < 300


# What an MCP streamable-HTTP endpoint may answer to a plain GET that is not an MCP exchange.
# 401, 403 and 404 are left out: a wrong token, a refused caller or a wrong path means the Hub
# cannot use the server, so it is not ready.
_MCP_REACHABLE_STATUSES = frozenset({400, 405, 406})


def _mcp_answered(status: int) -> bool:
    return _is_success(status) or status in _MCP_REACHABLE_STATUSES


def qdrant_check(
    qdrant_url: str, api_key: str = "", transport: httpx2.AsyncBaseTransport | None = None
) -> ReadinessCheck:
    # A local Qdrant answers /readyz without a key, but Qdrant Cloud wants one (P10-07), so the
    # key goes along when it is set. The port is the one the KB client uses (`qdrant_port`).
    headers = {"api-key": api_key} if api_key else None
    return http_check(
        f"{qdrant_port(qdrant_url)}/readyz",
        accept=_is_success,
        headers=headers,
        transport=transport,
    )


QDRANT_DEFAULT_PORT = 6333


def qdrant_port(qdrant_url: str) -> str:
    """The URL with the port qdrant-client uses: 6333 when the URL names none (qdrant-client
    1.20, `AsyncQdrantRemote`). On the first deploy (P10-07) the KB client worked on 6333
    while /readyz on the bare Qdrant Cloud URL (443) answered 403."""
    parts = urlsplit(qdrant_url.rstrip("/"))
    if parts.port is not None or parts.hostname is None:
        return qdrant_url.rstrip("/")
    return urlunsplit(parts._replace(netloc=f"{parts.netloc}:{QDRANT_DEFAULT_PORT}"))


def mcp_check(
    mcp_server_url: str,
    token: str,
    transport: httpx2.AsyncBaseTransport | None = None,
    extra_headers: Callable[[], Awaitable[dict[str, str]]] | None = None,
) -> ReadinessCheck:
    """Reachability only; the real MCP handshake is tested with the client in phase 7.
    `extra_headers`: the Cloud Run ID token header (P10-07), fetched on every check."""
    # Accept: application/json, so the server never opens an SSE stream for this GET.
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if extra_headers is None:
        return http_check(
            mcp_server_url, accept=_mcp_answered, headers=headers, transport=transport
        )
    add_headers = extra_headers

    async def check() -> None:
        found = http_check(
            mcp_server_url,
            accept=_mcp_answered,
            headers=headers | await add_headers(),
            transport=transport,
        )
        await found()

    return check


def kb_index_check(counts: Callable[[], Awaitable[tuple[int, int]]]) -> ReadinessCheck:
    """The index holds one point per chunk in Postgres (P5-01). `counts` returns
    (Postgres chunks, Qdrant points); a difference means `make kb-load` or a re-index is due."""

    async def check() -> dict[str, int]:
        postgres, qdrant = await counts()
        if postgres != qdrant:
            raise UnexpectedStatusError(f"chunks: postgres {postgres}, qdrant {qdrant}")
        return {"postgres_chunks": postgres, "qdrant_points": qdrant}

    return check
