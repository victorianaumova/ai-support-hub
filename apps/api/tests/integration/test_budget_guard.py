"""The daily budget guard (G8, P3-06) against Postgres, with recorded model answers.

Done when: with the budget set to 0, a new ticket is handed off (`budget_exceeded`) and no model
call is made. Only real-mode `llm_calls` rows of the current UTC day count, in every context
(`record` included); fake-mode rows never count (owner decisions, P3-01 and P3-05).
"""

import asyncio
import dataclasses
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from alembic import command
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import sql
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from hub.config import Settings
from hub.db.session import connect
from hub.deps import Container, build_pipeline
from hub.graph.pipeline import Pipeline
from hub.graph.run import run_ticket
from hub.kb.loader import load_kb, read_kb
from hub.llm.budget import BudgetGuard
from hub.llm.gateway import Provider
from hub.llm.schemas import BudgetExceededError, ProviderRequest, ProviderResponse
from hub.main import create_app
from hub.observability.readiness import libpq_url

from ._api import (
    ACCESS_CODE,
    SESSION_SECRET,
    Urls,
    add_customer,
    customer_client,
    key,
    own_database,
    run_sql,
)
from ._compose import ROOT
from ._db import alembic_config, as_user, migrate
from ._kb import RecordedSearch

DATABASE = "hub_budget_guard_test"
TICKETS = {
    json.loads(line)["id"]: json.loads(line)["ticket"]
    for line in (ROOT / "apps/api/tests/fixtures/run/tickets.jsonl").read_text().splitlines()
    if line.strip()
}
SEPA = TICKETS["run_01"]


@pytest.fixture(scope="module")
def budget_db(postgres_url: str) -> Iterator[Urls]:
    with own_database(postgres_url, DATABASE) as urls:

        async def load() -> None:
            engine = create_async_engine(urls[1])
            async with AsyncSession(engine) as session, session.begin():
                await load_kb(session, read_kb(ROOT / "data" / "kb"))
            await engine.dispose()

        asyncio.run(load())
        yield urls


@pytest.fixture(autouse=True)
def _empty(budget_db: Urls) -> None:
    run_sql(
        budget_db,
        "TRUNCATE audit_log, llm_calls, tool_calls, drafts, runs, feedback, messages, tickets,"
        " customers, operators, settings RESTART IDENTITY CASCADE",
    )
    add_customer(budget_db)


def _settings(urls: Urls) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        database_url=urls[1],
        session_secret=SESSION_SECRET,
        demo_access_code=ACCESS_CODE,
        llm_mode="fake",
        models_config=ROOT / "config" / "models.yaml",
        prompts_dir=ROOT / "prompts",
        daily_budget_usd=0.5,
    )


@dataclasses.dataclass
class SpyProvider:
    """The fake-mode provider, counting the requests that reach it."""

    inner: Provider
    requests: list[ProviderRequest] = dataclasses.field(default_factory=list)

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        return await self.inner.complete(request)

    async def embed(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        return await self.inner.embed(request)


def _spied(pipeline: Pipeline) -> tuple[Pipeline, SpyProvider]:
    spy = SpyProvider(pipeline.gateway.provider)
    return dataclasses.replace(
        pipeline, gateway=dataclasses.replace(pipeline.gateway, provider=spy)
    ), spy


@pytest.fixture
def app_and_spy(budget_db: Urls) -> Iterator[tuple[FastAPI, SpyProvider]]:
    settings = _settings(budget_db)
    database = connect(settings.database_url.get_secret_value())
    pipeline, spy = _spied(build_pipeline(settings, database, RecordedSearch()))
    application = create_app(Container(settings=settings, database=database, pipeline=pipeline))
    with TestClient(application):  # runs the lifespan: waits for runs at the end
        yield application, spy


def _set_budget(urls: Urls, value: object) -> None:
    run_sql(
        urls,
        "INSERT INTO settings (key, value) VALUES ('daily_budget_usd', %s::jsonb)",
        json.dumps(value),
    )


def _add_call(
    urls: Urls, *, mode: str, cost: str, context: str = "online", created_at: str = "now()"
) -> None:
    # `created_at` is SQL written in this file (a time expression), never outside input.
    columns = (
        "id, context, mode, node, model, input_tokens, output_tokens, cached_tokens, cost_usd,"
        " latency_ms, status, created_at"
    )
    values = f"%s, %s, %s, 'triage', 'claude-haiku-5-5', 1, 1, 0, %s, 1, 'ok', {created_at}"
    query = f"INSERT INTO llm_calls ({columns}) VALUES ({values})"  # noqa: S608
    run_sql(
        urls,
        query,
        uuid.uuid4(),
        context,
        mode,
        Decimal(cost),
    )


def _post(app: FastAPI) -> tuple[uuid.UUID, list[tuple[str, Any]]]:
    response = customer_client(app).post("/api/v1/tickets", json={"message": SEPA}, headers=key())
    assert response.status_code == 200, response.text
    events: list[tuple[str, Any]] = []
    for block in response.text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines:
            events.append((lines["event"], json.loads(lines["data"])))
    return uuid.UUID(events[0][1]["ticket_id"]), events


def _ticket(urls: Urls, ticket_id: uuid.UUID) -> tuple[Any, ...]:
    [row] = run_sql(
        urls, "SELECT status, last_route, route_reasons FROM tickets WHERE id = %s", ticket_id
    )
    return row


def _assert_budget_handoff(urls: Urls, ticket_id: uuid.UUID) -> None:
    assert _ticket(urls, ticket_id) == ("handed_off", "handoff", ["budget_exceeded"])
    [(status, route, reasons, error)] = run_sql(
        urls, "SELECT status, route, route_reasons, error FROM runs WHERE ticket_id = %s", ticket_id
    )
    assert (status, route, reasons, error) == (
        "failed",
        "handoff",
        ["budget_exceeded"],
        "BudgetExceededError",
    )
    [(details,)] = run_sql(
        urls,
        "SELECT details FROM audit_log WHERE action = 'route.decided' AND entity_id = %s",
        str(ticket_id),
    )
    assert (details["route"], details["reasons"]) == ("handoff", ["budget_exceeded"])
    assert run_sql(urls, "SELECT 1 FROM drafts WHERE ticket_id = %s", ticket_id) == []


# --- through the API --------------------------------------------------------------------------


def test_with_the_budget_at_zero_a_new_ticket_is_handed_off_without_a_model_call(
    app_and_spy: tuple[FastAPI, SpyProvider], budget_db: Urls
) -> None:
    """Done when of P3-06."""
    app, spy = app_and_spy
    _set_budget(budget_db, 0)

    ticket_id, events = _post(app)

    assert spy.requests == []
    assert run_sql(budget_db, "SELECT count(*) FROM llm_calls") == [(0,)]
    _assert_budget_handoff(budget_db, ticket_id)
    assert events[-2] == ("ticket.status", {"status": "handed_off"})
    assert "error" in [name for name, _ in events]  # same stream as any failed run, no new code


def test_fake_mode_rows_never_count(
    app_and_spy: tuple[FastAPI, SpyProvider], budget_db: Urls
) -> None:
    """Fake-mode rows carry the cost of their recording, but were never paid (P3-05)."""
    app, spy = app_and_spy
    _add_call(budget_db, mode="fake", cost="9.99")

    ticket_id, _ = _post(app)

    assert _ticket(budget_db, ticket_id)[0] == "pending_approval"
    # triage, write and verify (P5-04); the write/v6 recordings pass the check, no retry (P7-06)
    assert len(spy.requests) == 3
    modes = run_sql(budget_db, "SELECT DISTINCT mode FROM llm_calls WHERE run_id IS NOT NULL")
    assert modes == [("fake",)]


@pytest.mark.parametrize("context", ["online", "record", "memory"])
def test_real_rows_of_today_count_in_every_context(
    app_and_spy: tuple[FastAPI, SpyProvider], budget_db: Urls, context: str
) -> None:
    """`make record-llm` spend counts against the daily budget (P3-01). No settings row: the
    limit is DAILY_BUDGET_USD (0.50)."""
    app, spy = app_and_spy
    _add_call(budget_db, mode="real", cost="0.30", context=context)
    _add_call(budget_db, mode="real", cost="0.20", context=context)

    ticket_id, _ = _post(app)

    assert spy.requests == []
    _assert_budget_handoff(budget_db, ticket_id)


def test_the_settings_row_overrides_the_start_value(
    app_and_spy: tuple[FastAPI, SpyProvider], budget_db: Urls
) -> None:
    app, _ = app_and_spy
    _add_call(budget_db, mode="real", cost="0.60")
    _set_budget(budget_db, 1.0)

    ticket_id, _ = _post(app)

    assert _ticket(budget_db, ticket_id)[0] == "pending_approval"


# --- the run checks at its start, the gateway before every online call ---------------------


async def _run_with(pipeline: Pipeline, urls: Urls) -> uuid.UUID:
    """A ticket with one message, written outside the app, and one run for it."""
    customer_id = run_sql(urls, "SELECT id FROM customers")[0][0]
    ticket_id, message_id = uuid.uuid4(), uuid.uuid4()
    run_sql(
        urls,
        "INSERT INTO tickets (id, customer_id, channel, status, ai_disclosed_at)"
        " VALUES (%s, %s, 'chat', 'processing', now())",
        ticket_id,
        customer_id,
    )
    run_sql(
        urls,
        "INSERT INTO messages (id, ticket_id, role, body) VALUES (%s, %s, 'customer', %s)",
        message_id,
        ticket_id,
        SEPA,
    )

    async def emit(_name: str, _data: dict[str, Any]) -> None:
        return None

    await run_ticket(pipeline, ticket_id=ticket_id, message_id=message_id, emit=emit)
    return ticket_id


async def test_the_run_checks_the_budget_at_its_start(budget_db: Urls) -> None:
    """Even without the gateway's check, a run makes no model call once the budget is reached."""
    _set_budget(budget_db, 0)
    settings = _settings(budget_db)
    database = connect(settings.database_url.get_secret_value())
    try:
        pipeline, spy = _spied(build_pipeline(settings, database, RecordedSearch()))
        pipeline = dataclasses.replace(
            pipeline, gateway=dataclasses.replace(pipeline.gateway, budget=None)
        )
        ticket_id = await _run_with(pipeline, budget_db)
    finally:
        await database.dispose()
    assert spy.requests == []
    _assert_budget_handoff(budget_db, ticket_id)


@dataclasses.dataclass
class NoCheck:
    async def check(self) -> None:
        return None


async def test_the_apps_gateway_checks_the_budget_itself(budget_db: Urls) -> None:
    """The gateway of `build_pipeline` holds the same guard as the run, and refuses on its own
    when the run-start check is out of the way."""
    _set_budget(budget_db, 0)
    settings = _settings(budget_db)
    database = connect(settings.database_url.get_secret_value())
    try:
        built = build_pipeline(settings, database, RecordedSearch())
        assert isinstance(built.budget, BudgetGuard)
        assert built.gateway.budget is built.budget
        pipeline, spy = _spied(dataclasses.replace(built, budget=NoCheck()))
        ticket_id = await _run_with(pipeline, budget_db)
    finally:
        await database.dispose()
    assert spy.requests == []
    assert run_sql(budget_db, "SELECT count(*) FROM llm_calls") == [(0,)]
    _assert_budget_handoff(budget_db, ticket_id)


@dataclasses.dataclass
class RefuseAfter:
    allowed: int
    checks: int = 0

    async def check(self) -> None:
        self.checks += 1
        if self.checks > self.allowed:
            raise BudgetExceededError(Decimal("0.5"), Decimal("0.5"))


async def test_a_run_that_reaches_the_budget_half_way_is_handed_off(budget_db: Urls) -> None:
    """The run's start check and the triage call pass; the write call is refused."""
    settings = _settings(budget_db)
    database = connect(settings.database_url.get_secret_value())
    budget = RefuseAfter(allowed=2)
    try:
        pipeline, spy = _spied(build_pipeline(settings, database, RecordedSearch()))
        pipeline = dataclasses.replace(
            pipeline,
            budget=budget,
            gateway=dataclasses.replace(pipeline.gateway, budget=budget),
        )
        ticket_id = await _run_with(pipeline, budget_db)
    finally:
        await database.dispose()
    assert [request.schema_name for request in spy.requests] == ["TriageResult"]
    assert budget.checks == 3
    _assert_budget_handoff(budget_db, ticket_id)
    [(intent,)] = run_sql(budget_db, "SELECT intent FROM tickets WHERE id = %s", ticket_id)
    assert intent is not None  # triage was saved before the stop


# --- the sum ----------------------------------------------------------------------------------


async def test_the_day_is_the_utc_calendar_day(budget_db: Urls) -> None:
    """Fixed times on both sides (rows and the guard's clock), so the test never depends on
    when it runs."""
    _add_call(budget_db, mode="real", cost="0.40", created_at="'2026-11-15 23:59:59.999+00'")
    _add_call(budget_db, mode="real", cost="0.10", created_at="'2026-11-16 00:00:00+00'")
    # 00:30 in Bratislava (UTC+1) is still the 15th in UTC: does not count.
    _add_call(budget_db, mode="real", cost="0.20", created_at="'2026-11-16 00:30:00+01'")
    _add_call(budget_db, mode="real", cost="0.05", created_at="'2026-11-16 17:00:00+00'")
    _add_call(budget_db, mode="fake", cost="0.70", created_at="'2026-11-16 12:00:00+00'")
    database = connect(_settings(budget_db).database_url.get_secret_value())
    try:
        guard = BudgetGuard(
            database,
            default_limit=Decimal("0.5"),
            now=lambda: datetime(2026, 11, 16, 18, tzinfo=UTC),
        )
        assert await guard.spent_today() == Decimal("0.15")
        await guard.check()  # 0.15 < 0.50
    finally:
        await database.dispose()


# --- the migration ----------------------------------------------------------------------------


@pytest.fixture
def fresh_db_url(postgres_url: str) -> Iterator[str]:
    name = "hub_budget_migration_test"
    with psycopg.connect(libpq_url(postgres_url), autocommit=True) as connection:
        connection.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    yield as_user(postgres_url, "hub", "hub", database=name)
    with psycopg.connect(libpq_url(postgres_url), autocommit=True) as connection:
        connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def test_rows_written_before_the_migration_become_real_and_new_rows_need_a_mode(
    fresh_db_url: str,
) -> None:
    migrate(fresh_db_url, "0007")
    with psycopg.connect(libpq_url(fresh_db_url), autocommit=True) as connection:
        connection.execute(
            "INSERT INTO llm_calls (id, context, node, model, input_tokens, output_tokens,"
            " cached_tokens, cost_usd, latency_ms, status)"
            " VALUES (gen_random_uuid(), 'online', 'triage', 'm', 1, 1, 0, 0.01, 1, 'ok')"
        )
    command.upgrade(alembic_config(fresh_db_url), "0008")
    with psycopg.connect(libpq_url(fresh_db_url), autocommit=True) as connection:
        assert connection.execute("SELECT mode FROM llm_calls").fetchall() == [("real",)]
        default = connection.execute(
            "SELECT column_default FROM information_schema.columns"
            " WHERE table_name = 'llm_calls' AND column_name = 'mode'"
        ).fetchone()
        assert default == (None,)
        with pytest.raises(psycopg.errors.NotNullViolation):
            connection.execute(
                "INSERT INTO llm_calls (id, context, node, model, input_tokens, output_tokens,"
                " cached_tokens, cost_usd, latency_ms, status)"
                " VALUES (gen_random_uuid(), 'online', 'triage', 'm', 1, 1, 0, 0.01, 1, 'ok')"
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                "INSERT INTO llm_calls (id, context, mode, node, model, input_tokens,"
                " output_tokens, cached_tokens, cost_usd, latency_ms, status)"
                " VALUES (gen_random_uuid(), 'online', 'paid', 'triage', 'm', 1, 1, 0, 0.01, 1,"
                " 'ok')"
            )
