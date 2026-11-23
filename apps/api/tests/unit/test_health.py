"""`/health` needs nothing; `/ready` reports each dependency and is 503 when one fails."""

import asyncio
from collections.abc import Callable, Coroutine

import httpx2
import pytest
from fastapi.testclient import TestClient

from hub.api import health
from hub.config import Settings
from hub.deps import build_container
from hub.observability.readiness import (
    UnexpectedStatusError,
    kb_index_check,
    libpq_url,
    mcp_check,
    postgres_check,
    qdrant_check,
)

MakeClient = Callable[..., TestClient]


async def ok() -> None:
    return None


async def refused() -> None:
    raise ConnectionRefusedError("postgresql://hub:secret@db/hub refused")


async def hangs() -> None:
    await asyncio.sleep(10)


def test_health_needs_no_dependency(make_client: MakeClient) -> None:
    response = make_client({"postgres": refused}).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_when_every_check_passes(make_client: MakeClient) -> None:
    response = make_client({"postgres": ok, "qdrant": ok, "mcp": ok}).get("/ready")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {
            "postgres": {"status": "ok"},
            "qdrant": {"status": "ok"},
            "mcp": {"status": "ok"},
        },
    }


def test_not_ready_names_the_failing_check_without_its_message(make_client: MakeClient) -> None:
    response = make_client({"postgres": refused, "qdrant": ok}).get("/ready")
    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {
            "postgres": {"status": "error", "error": "ConnectionRefusedError"},
            "qdrant": {"status": "ok"},
        },
    }
    assert "secret" not in response.text


def test_hanging_check_times_out(make_client: MakeClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "CHECK_TIMEOUT_SECONDS", 0.05)
    response = make_client({"qdrant": hangs}).get("/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["qdrant"] == {"status": "error", "error": "timeout"}


def test_container_checks_postgres_qdrant_the_kb_index_and_mcp() -> None:
    container = build_container(Settings(_env_file=None))
    assert set(container.readiness_checks) == {"postgres", "qdrant", "kb_index", "mcp"}
    assert container.kb is not None


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "postgresql+psycopg://hub:hub@localhost:5432/hub",
            "postgresql://hub:hub@localhost:5432/hub",
        ),
        ("postgresql://hub@db/hub", "postgresql://hub@db/hub"),
    ],
)
def test_libpq_url_drops_the_driver(url: str, expected: str) -> None:
    assert libpq_url(url) == expected


class FakeConnection:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.closed = False

    async def __aenter__(self) -> "FakeConnection":
        return self

    async def __aexit__(self, *_: object) -> None:
        self.closed = True

    async def execute(self, query: str) -> None:
        self.queries.append(query)


async def test_postgres_check_runs_select_1_and_closes() -> None:
    connection = FakeConnection()
    calls: list[tuple[str, dict[str, object]]] = []

    async def connect(url: str, **kwargs: object) -> FakeConnection:
        calls.append((url, kwargs))
        return connection

    await postgres_check("postgresql+psycopg://hub@db/hub", connect=connect)()
    assert calls == [("postgresql://hub@db/hub", {"connect_timeout": 2})]
    assert connection.queries == ["SELECT 1"]
    assert connection.closed


def _transport(status: int, seen: list[httpx2.Request]) -> httpx2.MockTransport:
    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(status)

    return httpx2.MockTransport(handle)


async def test_qdrant_check_passes_when_the_kb_client_answers() -> None:
    calls: list[str] = []

    async def answered() -> str | None:
        calls.append("collection")
        return None  # no index yet is still an answer; kb_index reports the counts

    assert await qdrant_check(answered)() is None
    assert calls == ["collection"]


async def test_qdrant_check_fails_when_the_kb_client_fails() -> None:
    async def refused() -> str | None:
        raise RuntimeError("Unexpected Response: 403 (Forbidden)")

    with pytest.raises(RuntimeError, match="403"):
        await qdrant_check(refused)()


@pytest.mark.parametrize("status", [200, 202, 400, 405, 406])
async def test_mcp_check_accepts_answers_of_a_reachable_server(status: int) -> None:
    seen: list[httpx2.Request] = []
    await mcp_check("http://mcp:8001/mcp", "tok", transport=_transport(status, seen))()
    assert seen[0].headers["authorization"] == "Bearer tok"
    assert seen[0].headers["accept"] == "application/json"


@pytest.mark.parametrize("status", [302, 401, 403, 404, 500, 502])
async def test_mcp_check_fails_when_the_hub_could_not_use_the_server(status: int) -> None:
    with pytest.raises(UnexpectedStatusError, match=f"HTTP {status}"):
        await mcp_check("http://mcp:8001/mcp", "tok", transport=_transport(status, []))()


async def test_mcp_check_sends_no_auth_without_token() -> None:
    seen: list[httpx2.Request] = []
    await mcp_check("http://mcp:8001/mcp", "", transport=_transport(200, seen))()
    assert "authorization" not in seen[0].headers


def test_ready_shows_the_status_of_a_wrong_answer(make_client: MakeClient) -> None:
    async def unauthorised() -> None:
        raise UnexpectedStatusError("HTTP 401")

    response = make_client({"mcp": unauthorised}).get("/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["mcp"] == {"status": "error", "error": "HTTP 401"}


# --- the KB index check (P5-01) ----------------------------------------------------------------


def _counts(postgres: int, qdrant: int) -> Callable[[], Coroutine[None, None, tuple[int, int]]]:
    async def counts() -> tuple[int, int]:
        return postgres, qdrant

    return counts


def test_ready_shows_the_index_counts_when_they_match(make_client: MakeClient) -> None:
    response = make_client({"kb_index": kb_index_check(_counts(120, 120))}).get("/ready")
    assert response.status_code == 200
    assert response.json()["checks"]["kb_index"] == {
        "status": "ok",
        "postgres_chunks": 120,
        "qdrant_points": 120,
    }


def test_ready_fails_when_postgres_and_qdrant_differ(make_client: MakeClient) -> None:
    response = make_client({"kb_index": kb_index_check(_counts(120, 118))}).get("/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["kb_index"] == {
        "status": "error",
        "error": "chunks: postgres 120, qdrant 118",
    }
