"""Memory between tickets (P6-05, ai-pipeline §9, ADR-0012) against Postgres and Qdrant.

Closing a ticket in the console writes a short summary to `customer_memory` (one call of the
`memory_summary` prompt; here a scripted answer, every other call from the fake-mode
recordings). The next run of the same customer loads it in `intake` (`customer_context`), and a
repeat contact on the same intent goes to approval (`memory_shows_open_case`). The console
ticket detail shows the customer's memory.
"""

import asyncio
import contextlib
import dataclasses
import json
import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from hub.db.session import connect
from hub.deps import Container, build_pipeline
from hub.domain.memory import CustomerContext
from hub.graph.context import GraphContext
from hub.graph.nodes import decide as decide_node
from hub.graph.nodes import intake as intake_node
from hub.graph.state import run_input
from hub.llm.gateway import FIXTURES_DIR, FixtureProvider
from hub.llm.schemas import BudgetExceededError, ProviderRequest, ProviderResponse, Usage
from hub.main import create_app
from hub.memory import summary as memory_summary
from hub.memory.summary import write_summary

from ._api import OPERATOR_PASSWORD, Urls, add_customer, add_operator, own_database, run_sql
from .test_graph_run import SEPA, _container, _index_kb, _kb_services, _one, _post, _settings
from .test_human_review import _customer, _detail, _snapshot

DATABASE = "hub_memory_test"
OPERATOR = "marta@tallybridge.test"
TEST_CARD = "4111 1111 1111 1111"  # public test card number
SUMMARY = "A SEPA withdrawal of 200 EUR had not arrived; support explained the times."


@pytest.fixture(scope="module")
def memory_db(postgres_url: str, qdrant_url: str) -> Iterator[Urls]:
    with own_database(postgres_url, DATABASE) as urls:
        _index_kb(urls, qdrant_url)
        yield urls


@pytest.fixture
def fresh(memory_db: Urls) -> Urls:
    run_sql(
        memory_db,
        "TRUNCATE audit_log, llm_calls, tool_calls, drafts, runs, feedback, messages, tickets,"
        " customers, customer_memory, operators, checkpoints, checkpoint_blobs, checkpoint_writes"
        " RESTART IDENTITY CASCADE",
    )
    add_operator(memory_db, OPERATOR)
    return memory_db


MEMORY_BLOCK = re.compile(r"<memory>\n(.*?)\n</memory>", re.DOTALL)
NO_MEMORY_BLOCK = "<memory>\n(none)\n</memory>"


@dataclasses.dataclass
class MemoryAnswers:
    """Answers the memory calls with scripted summaries and every other call from the fake-mode
    recordings."""

    summaries: list[str]
    seen: list[str] = dataclasses.field(default_factory=list)
    # The `<memory>` blocks of the write requests (write/v3, P7-04).
    memory_blocks: list[str] = dataclasses.field(default_factory=list)
    fixtures: FixtureProvider = dataclasses.field(
        default_factory=lambda: FixtureProvider(FIXTURES_DIR)
    )

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        system = str(request.messages[0]["content"])
        if "memory note" not in system:
            return await self.fixtures.complete(self._without_memory(request))
        self.seen.append(str(request.messages[-1]["content"]))
        content = json.dumps({"summary": self.summaries.pop(0)})
        return ProviderResponse(content=content, usage=Usage(input_tokens=50, output_tokens=20))

    async def embed(self, request: ProviderRequest) -> ProviderResponse:
        return await self.fixtures.embed(request)

    def _without_memory(self, request: ProviderRequest) -> ProviderRequest:
        """A write request with memory items is kept for the test and answered with the
        recording of the same request without them (the recordings have no memory)."""
        messages = list(request.messages)
        user = messages[-1].get("content")
        if not isinstance(user, str):
            return request
        found = MEMORY_BLOCK.search(user)
        if found is None or found.group(1) == "(none)":
            return request
        self.memory_blocks.append(found.group(1))
        messages[-1] = {**messages[-1], "content": MEMORY_BLOCK.sub(NO_MEMORY_BLOCK, user)}
        return request.model_copy(update={"messages": messages})


def _with(container: Container, provider: MemoryAnswers) -> Container:
    pipeline = container.pipeline
    assert pipeline is not None
    gateway = dataclasses.replace(pipeline.gateway, provider=provider)
    return dataclasses.replace(container, pipeline=dataclasses.replace(pipeline, gateway=gateway))


@contextlib.contextmanager
def _web(urls: Urls, qdrant_url: str, provider: MemoryAnswers) -> Iterator[TestClient]:
    application = create_app(_with(_container(urls, qdrant_url), provider))
    with TestClient(application, base_url="https://testserver") as client:
        yield client


def _login(web: TestClient) -> TestClient:
    response = web.post(
        "/api/v1/auth/login", json={"email": OPERATOR, "password": OPERATOR_PASSWORD}
    )
    assert response.status_code == 200, response.text
    web.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return web


def _answered_ticket(
    urls: Urls, customer_id: uuid.UUID, text: str, *, intent: str = "withdrawal_issue"
) -> uuid.UUID:
    """A ticket the customer wrote and support answered, written outside the app."""
    ticket_id = uuid.uuid4()
    run_sql(
        urls,
        "INSERT INTO tickets (id, customer_id, channel, status, intent, ai_disclosed_at)"
        " VALUES (%s, %s, 'chat', 'answered', %s, now())",
        ticket_id,
        customer_id,
        intent,
    )
    for role, body in (("customer", text), ("operator", "SEPA withdrawals take 1 business day.")):
        run_sql(
            urls,
            "INSERT INTO messages (id, ticket_id, role, body) VALUES (%s, %s, %s, %s)",
            uuid.uuid4(),
            ticket_id,
            role,
            body,
        )
    return ticket_id


def _close(operator: TestClient, ticket_id: uuid.UUID) -> None:
    response = operator.post(
        f"/api/v1/console/tickets/{ticket_id}/close",
        json={"ticket_version": _detail(operator, ticket_id)["version"]},
    )
    assert response.status_code == 200, response.text


def test_a_second_ticket_of_the_customer_has_the_first_ones_summary_in_context(
    fresh: Urls, qdrant_url: str
) -> None:
    """The "Done when" of P6-05, through the API: close → memory → the next run loads it, and
    a repeat contact on the same intent goes to approval with `policy_review`."""
    customer_id = add_customer(fresh)
    first = _answered_ticket(fresh, customer_id, "My SEPA withdrawal of 200 EUR is missing.")
    provider = MemoryAnswers([SUMMARY])
    with _web(fresh, qdrant_url, provider) as web:
        _close(_login(web), first)
        second, _ = _post(_customer(web), SEPA)
        detail = _detail(web, second)

    [(content, kind, memory_ticket)] = run_sql(
        fresh,
        "SELECT content, kind, ticket_id FROM customer_memory WHERE customer_id = %s",
        customer_id,
    )
    assert (content, kind, memory_ticket) == (SUMMARY, "ticket_summary", first)
    # the memory model read the conversation, roles first
    assert "customer: My SEPA withdrawal of 200 EUR is missing." in provider.seen[0]

    values = asyncio.run(_snapshot(fresh, second)).values
    context = values["customer_context"]
    assert isinstance(context, CustomerContext)
    assert [item.summary for item in context.memory] == [SUMMARY]
    assert context.memory[0].intent == "withdrawal_issue"
    # write/v3 (P7-04): the writer reads the memory item, as context, in every write call
    assert provider.memory_blocks
    assert all(f"(withdrawal_issue): {SUMMARY}" in block for block in provider.memory_blocks)
    # the recorded SEPA run: triage withdrawal_issue; with the write/v5 recordings (P7-06) the
    # check passes without a retry, so the memory's open case on the intent decides (row 6).
    assert values["triage"].intent == "withdrawal_issue"
    assert values["retry_count"] == 0
    assert decide_node.open_case(values, datetime.now(UTC))
    assert (values["route"], values["route_reasons"]) == ("approval", ["policy_review"])

    assert [item["summary"] for item in detail["memory"]] == [SUMMARY]
    assert detail["memory"][0]["ticket_id"] == str(first)
    # "Why you see this ticket" (P12): the repeat contact and the closed ticket it repeats
    assert detail["review"]["rules"] == ["memory_shows_open_case"]
    [repeat] = detail["review"]["repeat_of"]
    assert (repeat["id"], repeat["intent"], repeat["status"]) == (
        str(first),
        "withdrawal_issue",
        "closed",
    )
    intake_details = _one(fresh, "SELECT details FROM audit_log WHERE action = 'intake.done'")[0]
    assert (intake_details["memory_items"], intake_details["open_tickets"]) == (1, 0)


def test_the_console_links_the_open_ticket_a_repeat_contact_repeats(
    fresh: Urls, qdrant_url: str
) -> None:
    """P12: a draft sent to approval because the customer has another open ticket on the same
    intent names the rule and links that ticket with its intent and status. A ticket created
    after the run, or on another intent, is not linked; a ticket without `policy_review` has
    no review."""
    customer_id = add_customer(fresh)
    first = _answered_ticket(fresh, customer_id, "My SEPA withdrawal of 200 EUR is missing.")
    _answered_ticket(fresh, customer_id, "What does a card payment abroad cost?", intent="fees_fx")
    with _web(fresh, qdrant_url, MemoryAnswers([])) as web:
        second, _ = _post(_customer(web), SEPA)
        later = _answered_ticket(fresh, customer_id, "Still no SEPA withdrawal.")
        operator = _login(web)
        detail = _detail(operator, second)
        first_detail = _detail(operator, first)

    assert detail["route_reasons"] == ["policy_review"]
    assert detail["review"]["rules"] == ["memory_shows_open_case"]
    assert [(t["id"], t["intent"], t["status"]) for t in detail["review"]["repeat_of"]] == [
        (str(first), "withdrawal_issue", "answered")
    ]
    assert str(later) not in json.dumps(detail["review"])
    assert first_detail["review"] is None


def test_the_summary_is_recorded_masked_and_never_holds_card_data(
    fresh: Urls, qdrant_url: str
) -> None:
    customer_id = add_customer(fresh)
    # stored as the API stores it (G1); a full number would be stopped by G2 before the model
    ticket_id = _answered_ticket(fresh, customer_id, "My card [CARD ****1111] was declined.")
    written = f"Card {TEST_CARD} was declined; the reply went to <EMAIL_1>."
    provider = MemoryAnswers([written])
    with _web(fresh, qdrant_url, provider) as web:
        _close(_login(web), ticket_id)

    # the model wrote a full card number anyway: masked before it is stored
    (content,) = _one(fresh, "SELECT content FROM customer_memory WHERE ticket_id = %s", ticket_id)
    assert content == "Card [CARD ****1111] was declined; the reply went to [REDACTED]."
    audit = _one(fresh, "SELECT details FROM audit_log WHERE action = 'memory.written'")[0]
    assert audit["masked"] == {"PAN": 1}
    assert audit["prompt_version"] == "memory_summary/v1"
    everything = run_sql(
        fresh,
        "SELECT (SELECT string_agg(content, ' ') FROM customer_memory)"
        " || (SELECT string_agg(details::text, ' ') FROM audit_log)",
    )
    assert "4111 1111" not in str(everything)
    context, node, run_id, version = _one(
        fresh,
        "SELECT context, node, run_id, prompt_version FROM llm_calls WHERE node = %s",
        "memory_summary",
    )
    assert (context, node, run_id, version) == (
        "memory",
        "memory_summary",
        None,
        "memory_summary/v1",
    )


async def _intake_context(urls: Urls, qdrant_url: str, ticket_id: uuid.UUID) -> CustomerContext:
    settings = _settings(urls, qdrant_url)
    database = connect(settings.database_url.get_secret_value())
    kb = _kb_services(settings, database)
    pipeline = build_pipeline(settings, database, kb.retriever)
    message_id = uuid.uuid4()
    run_sql(
        urls,
        "INSERT INTO messages (id, ticket_id, role, body) VALUES (%s, %s, 'customer', 'Hi')",
        message_id,
        ticket_id,
    )
    state = run_input(
        ticket_id=str(ticket_id),
        run_id=str(uuid.uuid4()),
        message_id=str(message_id),
        customer_ref=str(uuid.uuid4()),
    )

    async def emit(name: str, data: dict[str, Any]) -> None:
        return None

    try:
        update = await intake_node.run(state, GraphContext(pipeline=pipeline, emit=emit))
    finally:
        await pipeline.checkpoints.close()
        await kb.close()
        await database.dispose()
    context = update["customer_context"]
    assert context is not None
    return context


def _memory(urls: Urls, customer_id: uuid.UUID, ticket_id: uuid.UUID, age: str, text: str) -> None:
    run_sql(
        urls,
        "INSERT INTO customer_memory (id, customer_id, ticket_id, kind, content, created_at,"
        " expires_at) VALUES (%s, %s, %s, 'ticket_summary', %s, now() - %s::interval,"
        " now() - %s::interval + interval '12 months')",
        uuid.uuid4(),
        customer_id,
        ticket_id,
        text,
        age,
        age,
    )


async def test_intake_loads_the_five_newest_unexpired_items_and_the_other_open_tickets(
    fresh: Urls, qdrant_url: str
) -> None:
    customer_id = add_customer(fresh)
    other_customer = add_customer(fresh, "CUS-0002", "Bo Berg")
    for age in ("1 day", "2 days", "3 days", "4 days", "5 days", "6 days", "13 months"):
        ticket = _answered_ticket(fresh, customer_id, "Old question.")
        run_sql(fresh, "UPDATE tickets SET status = 'closed' WHERE id = %s", ticket)
        _memory(fresh, customer_id, ticket, age, f"Summary {age} old.")
    foreign = _answered_ticket(fresh, other_customer, "Not yours.")
    _memory(fresh, other_customer, foreign, "1 hour", "Another customer's summary.")
    still_open = _answered_ticket(fresh, customer_id, "Open question.", intent="fees_fx")
    current = _answered_ticket(fresh, customer_id, "New question.")
    run_sql(fresh, "UPDATE tickets SET status = 'processing', intent = NULL WHERE id = %s", current)

    context = await _intake_context(fresh, qdrant_url, current)

    assert [item.summary for item in context.memory] == [
        f"Summary {age} old." for age in ("1 day", "2 days", "3 days", "4 days", "5 days")
    ]
    assert [(t.ticket_id, t.intent, t.status) for t in context.open_tickets] == [
        (str(still_open), "fees_fx", "answered")
    ]


async def test_a_spent_budget_writes_no_summary(fresh: Urls, qdrant_url: str) -> None:
    class Spent:
        async def check(self) -> None:
            raise BudgetExceededError(Decimal("5"), Decimal("5"))

    customer_id = add_customer(fresh)
    ticket_id = _answered_ticket(fresh, customer_id, "Where is my withdrawal?")
    settings = _settings(fresh, qdrant_url)
    database = connect(settings.database_url.get_secret_value())
    pipeline = build_pipeline(settings, database)
    provider = MemoryAnswers([SUMMARY])
    gateway = dataclasses.replace(pipeline.gateway, provider=provider)
    assert pipeline.memory_prompt is not None
    try:
        await write_summary(database, gateway, pipeline.memory_prompt, ticket_id, budget=Spent())
    finally:
        await pipeline.checkpoints.close()
        await database.dispose()

    assert provider.seen == []
    assert run_sql(fresh, "SELECT id FROM customer_memory") == []
    (details,) = _one(fresh, "SELECT details FROM audit_log WHERE action = 'memory.failed'")
    assert details == {"error": "BudgetExceededError"}


async def test_a_ticket_is_summarised_once(fresh: Urls, qdrant_url: str) -> None:
    customer_id = add_customer(fresh)
    ticket_id = _answered_ticket(fresh, customer_id, "Where is my withdrawal?")
    settings = _settings(fresh, qdrant_url)
    database = connect(settings.database_url.get_secret_value())
    pipeline = build_pipeline(settings, database)
    provider = MemoryAnswers([SUMMARY, "A second summary."])
    gateway = dataclasses.replace(pipeline.gateway, provider=provider)
    assert pipeline.memory_prompt is not None
    try:
        for _ in range(2):
            await write_summary(database, gateway, pipeline.memory_prompt, ticket_id)
    finally:
        await pipeline.checkpoints.close()
        await database.dispose()

    assert len(provider.seen) == 1
    assert run_sql(fresh, "SELECT content FROM customer_memory") == [(SUMMARY,)]


def test_a_failed_summary_never_fails_the_close(fresh: Urls, qdrant_url: str) -> None:
    """Fake mode has no recording for a memory call: the close still succeeds."""
    customer_id = add_customer(fresh)
    ticket_id = _answered_ticket(fresh, customer_id, "Where is my withdrawal?")
    with contextlib.ExitStack() as stack:
        application = create_app(_container(fresh, qdrant_url))
        web = stack.enter_context(TestClient(application, base_url="https://testserver"))
        _close(_login(web), ticket_id)
    (status,) = _one(fresh, "SELECT status FROM tickets WHERE id = %s", ticket_id)
    assert status == "closed"
    assert run_sql(fresh, "SELECT id FROM customer_memory") == []
    (details,) = _one(fresh, "SELECT details FROM audit_log WHERE action = 'memory.failed'")
    assert details == {"error": "FixtureMissingError"}


RESTRICTION_REASON = "AML review: incoming funds from a high-risk jurisdiction"


def test_a_restricted_customers_summary_never_sees_internal_text(
    fresh: Urls, qdrant_url: str
) -> None:
    """Owner decision (PR #38 review): the memory model gets only what the customer saw. The
    handoff notice (`system`), the operator summary (`tickets.summary`) and the draft's missing
    information hold internal text; here they hold a restriction reason, which must not reach
    the model."""
    customer_id = add_customer(fresh, "CUS-0009", "Rita Restricted")
    ticket_id = uuid.uuid4()
    run_sql(
        fresh,
        "INSERT INTO tickets (id, customer_id, channel, status, intent, summary, ai_disclosed_at)"
        " VALUES (%s, %s, 'chat', 'handed_off', 'account_restricted', %s, now())",
        ticket_id,
        customer_id,
        f"Restricted: {RESTRICTION_REASON}",
    )
    for role, body in (
        ("customer", "Why can I not send money any more?"),
        ("system", f"Handed off (restricted_account). Internal: {RESTRICTION_REASON}"),
        ("operator", "Your account is under review. We will write to you when it is done."),
    ):
        run_sql(
            fresh,
            "INSERT INTO messages (id, ticket_id, role, body) VALUES (%s, %s, %s, %s)",
            uuid.uuid4(),
            ticket_id,
            role,
            body,
        )
    provider = MemoryAnswers(
        ["The customer asked why payments stopped; support said a review runs."]
    )
    with _web(fresh, qdrant_url, provider) as web:
        _close(_login(web), ticket_id)

    [request] = provider.seen
    assert "Why can I not send money any more?" in request
    assert "under review" in request
    assert "AML" not in request
    assert "system:" not in request
    assert run_sql(fresh, "SELECT count(*) FROM customer_memory") == [(1,)]


def test_a_summary_that_takes_too_long_is_recorded_and_the_close_succeeds(
    fresh: Urls, qdrant_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner decision (PR #38 review): a timeout on the memory call; the close still succeeds
    and the abandoned call has its `llm_calls` row."""

    class Slow(MemoryAnswers):
        async def complete(self, request: ProviderRequest) -> ProviderResponse:
            if "memory note" in str(request.messages[0]["content"]):
                await asyncio.sleep(5)
            return await super().complete(request)

    monkeypatch.setattr(memory_summary, "SUMMARY_TIMEOUT_S", 0.2)
    customer_id = add_customer(fresh)
    ticket_id = _answered_ticket(fresh, customer_id, "Where is my withdrawal?")
    with _web(fresh, qdrant_url, Slow([SUMMARY])) as web:
        _close(_login(web), ticket_id)

    assert _one(fresh, "SELECT status FROM tickets WHERE id = %s", ticket_id) == ("closed",)
    assert run_sql(fresh, "SELECT id FROM customer_memory") == []
    context, node, status, error, run_id = _one(
        fresh, "SELECT context, node, status, error, run_id FROM llm_calls"
    )
    assert (context, node, status, error, run_id) == (
        "memory",
        "memory_summary",
        "error",
        "CallerTimeout",
        None,
    )
    (details,) = _one(fresh, "SELECT details FROM audit_log WHERE action = 'memory.failed'")
    assert details == {"error": "TimeoutError"}
