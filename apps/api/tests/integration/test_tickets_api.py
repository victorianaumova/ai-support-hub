"""Customer ticket API against Postgres as the app role (P2-06).

Uses a database of its own: the API commits, and `verify_chain` needs an audit log with only
chained rows. Every test gets a fresh app (fresh rate limits) and empty tables.
"""

import asyncio
import dataclasses
import json
import threading
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from psycopg import sql
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from uuid_utils.compat import uuid7

from hub.api import tickets as tickets_module
from hub.api.limits import RateLimiter
from hub.audit import ChainCheck, verify_chain
from hub.config import Settings
from hub.db.models import Message
from hub.db.repositories import FeedbackRepository, MessageRepository
from hub.db.session import connect
from hub.deps import Container
from hub.guardrails import masking
from hub.guardrails.masking import MaskResult
from hub.main import create_app
from hub.observability.readiness import libpq_url

from ._db import app_url, as_user, migrate

DATABASE = "hub_tickets_api_test"
ACCESS_CODE = "demo-code-for-tests"
SESSION_SECRET = "session-secret-for-tests-only-0123456789"  # noqa: S105 - test value
# Public test card number (Visa test range); never a real card.
TEST_CARD = "4111 1111 1111 1111"
RAW_DIGITS = "4111111111111111"
ANSWERED_BY_AI = "update tickets set status = 'answered' where id = %s"


@pytest.fixture(scope="module")
def api_db(postgres_url: str) -> Iterator[tuple[str, str]]:
    """(migrator URL, app URL) of a migrated database of its own."""
    with psycopg.connect(libpq_url(postgres_url), autocommit=True) as connection:
        connection.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(DATABASE)))
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DATABASE)))
    migrator = as_user(postgres_url, "hub", "hub", database=DATABASE)
    migrate(migrator)
    yield migrator, app_url(postgres_url, database=DATABASE)
    with psycopg.connect(libpq_url(postgres_url), autocommit=True) as connection:
        connection.execute(
            sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(DATABASE))
        )


def _sql(api_db: tuple[str, str], query: str, *params: Any) -> list[tuple[Any, ...]]:  # noqa: ANN401
    """Run SQL as the migrator (the test's view of the database, outside the app)."""
    with psycopg.connect(libpq_url(api_db[0]), autocommit=True) as connection:
        cursor = connection.execute(query.encode(), params)
        return cursor.fetchall() if cursor.description else []


@pytest.fixture(autouse=True)
def empty_tables(api_db: tuple[str, str]) -> None:
    _sql(
        api_db,
        "TRUNCATE audit_log, feedback, llm_calls, tool_calls, drafts, runs, customer_memory,"
        " messages, tickets, customers RESTART IDENTITY",
    )


@pytest.fixture
def customers(api_db: tuple[str, str]) -> dict[str, uuid.UUID]:
    ids = {}
    for ref in ("CUS-0001", "CUS-0002", "CUS-0003"):
        ids[ref] = uuid.UUID(str(uuid7()))
        _sql(
            api_db,
            "INSERT INTO customers (id, external_ref, display_name, email, tier, country)"
            " VALUES (%s, %s, %s, %s, 'verified', 'DE')",
            ids[ref],
            ref,
            f"Customer {ref}",
            f"{ref.lower()}@example.com",
        )
    _sql(api_db, "UPDATE customers SET anonymised_at = now() WHERE external_ref = 'CUS-0003'")
    return ids


@pytest.fixture
def app_container(api_db: tuple[str, str]) -> Container:
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=api_db[1],
        session_secret=SESSION_SECRET,
        demo_access_code=ACCESS_CODE,
    )
    return Container(settings=settings, database=connect(api_db[1]))


@pytest.fixture
def app_client(app_container: Container) -> Iterator[TestClient]:
    # https: the session cookie is `Secure`, so the client sends it only over https.
    with TestClient(
        create_app(app_container), base_url="https://testserver", raise_server_exceptions=False
    ) as client:
        yield client


def _login(app: TestClient, ref: str = "CUS-0001") -> TestClient:
    """A client of its own (own cookie jar) with a demo session for `ref`."""
    client = TestClient(app.app, base_url="https://testserver", raise_server_exceptions=False)
    response = client.post(
        "/api/v1/demo/session", json={"customer_ref": ref, "access_code": ACCESS_CODE}
    )
    assert response.status_code == 200, response.text
    return client


def _key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _events(response: Response) -> list[tuple[str, Any]]:
    assert response.headers["content-type"].startswith("text/event-stream")
    events = []
    for block in response.text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((fields["event"], json.loads(fields["data"])))
    return events


async def _verify_chain(url: str) -> ChainCheck:
    engine = create_async_engine(url)
    try:
        async with AsyncSession(engine) as session:
            return await verify_chain(session)
    finally:
        await engine.dispose()


def _new_ticket(client: TestClient, message: str = "Where is my withdrawal?") -> dict[str, Any]:
    response = client.post("/api/v1/tickets", json={"message": message}, headers=_key())
    assert response.status_code == 200, response.text
    name, data = _events(response)[0]
    assert name == "ticket.created"
    return dict(data)


# --- masking and storage ---------------------------------------------------------------------


def test_card_number_is_stored_masked_and_never_in_the_database_or_logs(
    capsys: pytest.CaptureFixture[str],
    app_container: Container,
    api_db: tuple[str, str],
    customers: dict[str, uuid.UUID],
) -> None:
    # The app is built here, not in a fixture: its log handler binds sys.stdout when it is
    # built, and pytest swaps the captured stream between fixture setup and the test call.
    app = TestClient(create_app(app_container), base_url="https://testserver")
    client = _login(app)
    text = f"My card {TEST_CARD} was charged twice"
    response = client.post("/api/v1/tickets", json={"message": text}, headers=_key())
    assert response.status_code == 200

    events = _events(response)
    assert [name for name, _ in events] == ["ticket.created", "message", "done"]
    created = events[0][1]
    assert created["message"]["body"] == "My card [CARD ****1111] was charged twice"
    assert created["message"]["masked"] is True
    assert created["message"]["author_label"] == "You"
    assert events[1][1]["role"] == "system"
    assert events[2][1] == {}

    ticket_id = created["ticket_id"]
    follow_up = f"The number again: {TEST_CARD}"
    response = client.post(
        f"/api/v1/tickets/{ticket_id}/messages", json={"message": follow_up}, headers=_key()
    )
    assert response.status_code == 200

    stored = _sql(api_db, "SELECT body, masked_entities FROM messages ORDER BY created_at, id")
    assert stored == [
        ("My card [CARD ****1111] was charged twice", {"PAN": 1}),
        ("The number again: [CARD ****1111]", {"PAN": 1}),
    ]
    # The whole database, every table as text, and every log line: no raw card number.
    tables = [
        row[0]
        for row in _sql(api_db, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    ]
    for table in tables:
        dump = _sql(api_db, f"SELECT coalesce(string_agg(t::text, ' '), '') FROM {table} t")  # noqa: S608 - names from pg_tables
        assert RAW_DIGITS not in dump[0][0], table
        assert TEST_CARD not in dump[0][0], table
    logs = capsys.readouterr().out
    assert "request.completed" in logs
    assert RAW_DIGITS not in logs
    assert TEST_CARD not in logs
    assert "charged twice" not in logs  # no message text in the logs at all


def test_an_iban_is_stored_partly_masked_without_the_card_note(
    app_client: TestClient, api_db: tuple[str, str], customers: dict[str, uuid.UUID]
) -> None:
    # ADR-0017: the IBAN count goes to masked_entities and the audit record; the customer's
    # "masked" flag (the "We hid a card number" note) stays card-only.
    client = _login(app_client)
    created = _new_ticket(client, "Refund to DE89 3704 0044 0532 0130 00 please")
    assert created["message"]["body"] == "Refund to [IBAN DE89 \u20263000] please"
    assert created["message"]["masked"] is False
    assert _sql(api_db, "SELECT body, masked_entities FROM messages") == [
        ("Refund to [IBAN DE89 \u20263000] please", {"IBAN": 1})
    ]
    details = _sql(api_db, "SELECT details FROM audit_log WHERE action = 'guardrail.masked'")
    assert details == [({"counts": {"IBAN": 1}, "failed": False},)]


def test_an_iban_and_a_card_show_the_card_note(
    app_client: TestClient, api_db: tuple[str, str], customers: dict[str, uuid.UUID]
) -> None:
    client = _login(app_client)
    created = _new_ticket(client, f"Refund to DE89 3704 0044 0532 0130 00, card {TEST_CARD}")
    assert created["message"]["masked"] is True


def test_audit_records_ticket_message_and_masking_with_a_valid_chain(
    app_client: TestClient, api_db: tuple[str, str], customers: dict[str, uuid.UUID]
) -> None:
    client = _login(app_client)
    ticket = _new_ticket(client, f"card {TEST_CARD}")
    client.post(
        f"/api/v1/tickets/{ticket['ticket_id']}/messages",
        json={"message": "still waiting"},
        headers=_key(),
    )
    actions = [row[0] for row in _sql(api_db, "SELECT action FROM audit_log ORDER BY id")]
    assert actions == [
        "auth.demo_session",
        "ticket.created",
        "guardrail.masked",
        "message.received",
    ]
    details = _sql(api_db, "SELECT details FROM audit_log WHERE action = 'guardrail.masked'")
    assert details[0][0] == {"counts": {"PAN": 1}, "failed": False}

    result = asyncio.run(_verify_chain(api_db[1]))
    assert result.ok
    assert result.checked == 4


def test_over_long_message_is_rejected_and_nothing_is_stored(
    app_client: TestClient, api_db: tuple[str, str], customers: dict[str, uuid.UUID]
) -> None:
    client = _login(app_client)
    response = client.post("/api/v1/tickets", json={"message": "x" * 4001}, headers=_key())
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert _sql(api_db, "SELECT count(*) FROM messages") == [(0,)]
    assert _sql(api_db, "SELECT count(*) FROM tickets") == [(0,)]


# --- idempotency -----------------------------------------------------------------------------


def test_second_post_with_the_same_key_returns_the_same_message(
    app_client: TestClient, api_db: tuple[str, str], customers: dict[str, uuid.UUID]
) -> None:
    client = _login(app_client)
    key = _key()
    first = _events(client.post("/api/v1/tickets", json={"message": "hello"}, headers=key))
    second = _events(client.post("/api/v1/tickets", json={"message": "hello"}, headers=key))
    assert first[0] == second[0]
    assert _sql(api_db, "SELECT count(*) FROM tickets") == [(1,)]

    ticket_id = first[0][1]["ticket_id"]
    path = f"/api/v1/tickets/{ticket_id}/messages"
    key = _key()
    one = _events(client.post(path, json={"message": "more"}, headers=key))
    two = _events(client.post(path, json={"message": "more"}, headers=key))
    assert one[0] == two[0]
    assert _sql(api_db, "SELECT count(*) FROM messages") == [(2,)]


def test_same_key_for_a_different_request_is_a_conflict(
    app_client: TestClient, customers: dict[str, uuid.UUID]
) -> None:
    client = _login(app_client)
    key = _key()
    created = _events(client.post("/api/v1/tickets", json={"message": "hello"}, headers=key))
    ticket_id = created[0][1]["ticket_id"]
    other_text = client.post("/api/v1/tickets", json={"message": "other"}, headers=key)
    assert other_text.status_code == 409
    assert other_text.json()["code"] == "idempotency_key_reused"
    # The same key and text as a follow-up on that ticket is the same request: no new message.
    as_follow_up = client.post(
        f"/api/v1/tickets/{ticket_id}/messages", json={"message": "hello"}, headers=key
    )
    assert _events(as_follow_up)[0] == created[0]
    # The same key on another ticket is a different request.
    other_ticket = _new_ticket(client)["ticket_id"]
    elsewhere = client.post(
        f"/api/v1/tickets/{other_ticket}/messages", json={"message": "hello"}, headers=key
    )
    assert elsewhere.status_code == 409
    # A follow-up message's key, sent as a new ticket.
    key = _key()
    client.post(f"/api/v1/tickets/{ticket_id}/messages", json={"message": "again"}, headers=key)
    as_new_ticket = client.post("/api/v1/tickets", json={"message": "again"}, headers=key)
    assert as_new_ticket.status_code == 409


def test_same_key_from_another_customer_creates_their_own_ticket(
    app_client: TestClient, api_db: tuple[str, str], customers: dict[str, uuid.UUID]
) -> None:
    key = _key()
    first = _events(_login(app_client).post("/api/v1/tickets", json={"message": "a"}, headers=key))
    other = _events(
        _login(app_client, "CUS-0002").post("/api/v1/tickets", json={"message": "a"}, headers=key)
    )
    assert first[0][1]["ticket_id"] != other[0][1]["ticket_id"]
    assert _sql(api_db, "SELECT count(*) FROM tickets") == [(2,)]


def test_concurrent_duplicate_answers_with_the_stored_message(
    app_client: TestClient,
    api_db: tuple[str, str],
    customers: dict[str, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _login(app_client)
    key = _key()
    first = _events(client.post("/api/v1/tickets", json={"message": "race"}, headers=key))

    # The second request looks up the key before the first one committed: it sees nothing.
    real_lookup = MessageRepository.get_by_idempotency_key
    calls = 0

    async def late_lookup(self: MessageRepository, scoped: str) -> Message | None:
        nonlocal calls
        calls += 1
        return None if calls == 1 else await real_lookup(self, scoped)

    monkeypatch.setattr(MessageRepository, "get_by_idempotency_key", late_lookup)
    second = _events(client.post("/api/v1/tickets", json={"message": "race"}, headers=key))
    assert calls == 2
    assert second[0] == first[0]
    assert _sql(api_db, "SELECT count(*) FROM tickets") == [(1,)]
    assert _sql(api_db, "SELECT count(*) FROM audit_log WHERE action = 'ticket.created'") == [(1,)]


# --- customer isolation ----------------------------------------------------------------------


def test_another_customers_ticket_is_404_everywhere(
    app_client: TestClient, customers: dict[str, uuid.UUID]
) -> None:
    owner = _login(app_client)
    ticket_id = _new_ticket(owner)["ticket_id"]
    message_id = owner.get(f"/api/v1/tickets/{ticket_id}/messages").json()["items"][0]["id"]

    stranger = _login(app_client, "CUS-0002")
    calls = [
        ("GET", f"/api/v1/tickets/{ticket_id}", None),
        ("GET", f"/api/v1/tickets/{ticket_id}/messages", None),
        ("POST", f"/api/v1/tickets/{ticket_id}/messages", {"message": "hi"}),
        ("POST", f"/api/v1/tickets/{ticket_id}/request-human", None),
        (
            "POST",
            f"/api/v1/tickets/{ticket_id}/feedback",
            {"message_id": message_id, "rating": "up"},
        ),
    ]
    for method, path, body in calls:
        response = stranger.request(method, path, json=body, headers=_key())
        assert response.status_code == 404, path
        assert response.json()["code"] == "not_found"
    assert stranger.get("/api/v1/tickets").json()["items"] == []
    assert [t["id"] for t in owner.get("/api/v1/tickets").json()["items"]] == [ticket_id]


def test_a_session_of_an_anonymised_customer_stops_working(
    app_client: TestClient, api_db: tuple[str, str], customers: dict[str, uuid.UUID]
) -> None:
    client = _login(app_client)
    _sql(api_db, "UPDATE customers SET anonymised_at = now() WHERE external_ref = 'CUS-0001'")
    assert client.get("/api/v1/tickets").status_code == 401


# --- reading ---------------------------------------------------------------------------------


def test_get_ticket_and_messages_after(
    app_client: TestClient, customers: dict[str, uuid.UUID]
) -> None:
    client = _login(app_client)
    ticket_id = _new_ticket(client, "first")["ticket_id"]
    path = f"/api/v1/tickets/{ticket_id}"
    client.post(f"{path}/messages", json={"message": "second"}, headers=_key())

    ticket = client.get(path).json()
    assert ticket["status"] == "processing"
    assert ticket["ai_disclosure"].startswith("You are chatting with Tallybridge's AI assistant.")
    assert set(ticket) == {"id", "status", "ai_disclosure", "ai_disclosed_at", "created_at"}

    items = client.get(f"{path}/messages").json()["items"]
    assert [m["body"] for m in items] == ["first", "second"]
    assert set(items[0]) == {
        "id",
        "role",
        "author_label",
        "is_ai_generated",
        "body",
        "citations",
        "masked",
        "created_at",
    }
    after = client.get(f"{path}/messages", params={"after": items[0]["id"]}).json()["items"]
    assert [m["body"] for m in after] == ["second"]
    unknown = client.get(f"{path}/messages", params={"after": str(uuid.uuid4())})
    assert unknown.status_code == 404


def test_ticket_list_pages_with_a_cursor(
    app_client: TestClient, customers: dict[str, uuid.UUID]
) -> None:
    client = _login(app_client)
    ids = [_new_ticket(client, f"ticket {n}")["ticket_id"] for n in range(3)]
    page = client.get("/api/v1/tickets", params={"limit": 2}).json()
    assert [t["id"] for t in page["items"]] == [ids[2], ids[1]]
    rest = client.get("/api/v1/tickets", params={"limit": 2, "cursor": page["next_cursor"]}).json()
    assert [t["id"] for t in rest["items"]] == [ids[0]]
    assert rest["next_cursor"] is None


# --- status rules ----------------------------------------------------------------------------


def test_message_on_a_closed_ticket_is_409(
    app_client: TestClient, api_db: tuple[str, str], customers: dict[str, uuid.UUID]
) -> None:
    client = _login(app_client)
    ticket_id = _new_ticket(client)["ticket_id"]
    _sql(api_db, "UPDATE tickets SET status = 'closed' WHERE id = %s", ticket_id)
    response = client.post(
        f"/api/v1/tickets/{ticket_id}/messages", json={"message": "hi"}, headers=_key()
    )
    assert response.status_code == 409
    assert response.json()["code"] == "ticket_closed"
    human = client.post(f"/api/v1/tickets/{ticket_id}/request-human", headers=_key())
    assert human.status_code == 409


@pytest.mark.parametrize(
    ("last_route", "expected"),
    [
        (None, "processing"),
        ("auto_reply", "processing"),
        ("handoff", "handed_off"),
        ("approval", "handed_off"),
    ],
)
def test_message_on_an_answered_ticket_follows_ownership(
    app_client: TestClient,
    api_db: tuple[str, str],
    customers: dict[str, uuid.UUID],
    last_route: str | None,
    expected: str,
) -> None:
    client = _login(app_client)
    ticket_id = _new_ticket(client)["ticket_id"]
    _sql(
        api_db,
        "UPDATE tickets SET status = 'answered', last_route = %s WHERE id = %s",
        last_route,
        ticket_id,
    )
    client.post(f"/api/v1/tickets/{ticket_id}/messages", json={"message": "hi"}, headers=_key())
    assert client.get(f"/api/v1/tickets/{ticket_id}").json()["status"] == expected


def test_request_human_hands_off_once_and_is_audited(
    app_client: TestClient, api_db: tuple[str, str], customers: dict[str, uuid.UUID]
) -> None:
    client = _login(app_client)
    ticket_id = _new_ticket(client)["ticket_id"]
    path = f"/api/v1/tickets/{ticket_id}/request-human"
    first = client.post(path, headers=_key())
    assert first.status_code == 200
    assert first.json()["status"] == "handed_off"
    again = client.post(path, headers=_key())
    assert again.status_code == 200
    assert again.json()["status"] == "handed_off"
    assert _sql(api_db, "SELECT last_route, route_reasons FROM tickets") == [
        ("handoff", ["customer_request"])
    ]
    decided = _sql(api_db, "SELECT details FROM audit_log WHERE action = 'route.decided'")
    assert decided == [({"route": "handoff", "reasons": ["customer_request"]},)]


def test_request_human_on_pending_approval_changes_nothing(
    app_client: TestClient, api_db: tuple[str, str], customers: dict[str, uuid.UUID]
) -> None:
    client = _login(app_client)
    ticket_id = _new_ticket(client)["ticket_id"]
    _sql(api_db, "UPDATE tickets SET status = 'pending_approval' WHERE id = %s", ticket_id)
    response = client.post(f"/api/v1/tickets/{ticket_id}/request-human", headers=_key())
    assert response.json()["status"] == "pending_approval"


# --- feedback --------------------------------------------------------------------------------


def _assistant_message(api_db: tuple[str, str], ticket_id: str) -> str:
    message_id = str(uuid7())
    _sql(
        api_db,
        "INSERT INTO messages (id, ticket_id, role, body, is_ai_generated, citations)"
        " VALUES (%s, %s, 'assistant', 'Your withdrawal is on its way [1]', true, %s)",
        message_id,
        ticket_id,
        json.dumps(
            [
                {
                    "marker": 1,
                    "chunk_id": "withdrawal-times#1",
                    "doc_id": "withdrawal-times",
                    "title": "How long does a withdrawal take?",
                    "url": "/help/withdrawal-times",
                }
            ]
        ),
    )
    return message_id


def test_feedback_on_an_ai_answer_is_stored_with_a_masked_comment(
    app_client: TestClient, api_db: tuple[str, str], customers: dict[str, uuid.UUID]
) -> None:
    client = _login(app_client)
    ticket_id = _new_ticket(client)["ticket_id"]
    message_id = _assistant_message(api_db, ticket_id)

    view = client.get(f"/api/v1/tickets/{ticket_id}/messages").json()["items"][-1]
    assert view["author_label"] == "AI assistant"
    assert view["citations"] == [
        {"marker": 1, "title": "How long does a withdrawal take?", "url": "/help/withdrawal-times"}
    ]

    response = client.post(
        f"/api/v1/tickets/{ticket_id}/feedback",
        json={"message_id": message_id, "rating": "down", "comment": f"use card {TEST_CARD}"},
    )
    assert response.status_code == 201
    assert _sql(api_db, "SELECT rating, comment FROM feedback") == [
        ("down", "use card [CARD ****1111]")
    ]
    assert _sql(api_db, "SELECT count(*) FROM audit_log WHERE action = 'feedback.created'") == [
        (1,)
    ]


def test_feedback_only_on_ai_messages_of_the_same_ticket(
    app_client: TestClient, api_db: tuple[str, str], customers: dict[str, uuid.UUID]
) -> None:
    client = _login(app_client)
    ticket_id = _new_ticket(client)["ticket_id"]
    other_ticket = _new_ticket(client)["ticket_id"]
    customer_message = client.get(f"/api/v1/tickets/{ticket_id}/messages").json()["items"][0]
    not_ai = client.post(
        f"/api/v1/tickets/{ticket_id}/feedback",
        json={"message_id": customer_message["id"], "rating": "up"},
    )
    assert not_ai.status_code == 422
    assert not_ai.json()["code"] == "not_ai_message"
    elsewhere = _assistant_message(api_db, other_ticket)
    wrong_ticket = client.post(
        f"/api/v1/tickets/{ticket_id}/feedback", json={"message_id": elsewhere, "rating": "up"}
    )
    assert wrong_ticket.status_code == 404


# --- demo session and rate limits ------------------------------------------------------------


def test_demo_session_sets_a_secure_http_only_cookie(
    app_client: TestClient, customers: dict[str, uuid.UUID]
) -> None:
    response = app_client.post(
        "/api/v1/demo/session", json={"customer_ref": "CUS-0001", "access_code": ACCESS_CODE}
    )
    assert response.status_code == 200
    assert response.json() == {
        "customer_ref": "CUS-0001",
        "display_name": "Customer CUS-0001",
        "tier": "verified",
    }
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("hub_customer=")
    for attribute in ("HttpOnly", "Secure", "SameSite=lax", "Max-Age=28800", "Path=/"):
        assert attribute in cookie


@pytest.mark.parametrize("ref", ["CUS-0003", "CUS-0999"])  # anonymised, unknown
def test_demo_session_for_an_unknown_or_erased_customer_is_404(
    app_client: TestClient, customers: dict[str, uuid.UUID], ref: str
) -> None:
    response = app_client.post(
        "/api/v1/demo/session", json={"customer_ref": ref, "access_code": ACCESS_CODE}
    )
    assert response.status_code == 404


def test_wrong_access_codes_are_limited_to_5_per_15_minutes(
    app_client: TestClient, customers: dict[str, uuid.UUID]
) -> None:
    body = {"customer_ref": "CUS-0001", "access_code": "wrong"}
    for _ in range(5):
        response = app_client.post("/api/v1/demo/session", json=body)
        assert response.status_code == 401
        assert response.json()["code"] == "invalid_access_code"
    blocked = app_client.post(
        "/api/v1/demo/session", json={"customer_ref": "CUS-0001", "access_code": ACCESS_CODE}
    )
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0


def test_more_than_10_messages_a_minute_get_429(
    app_client: TestClient, api_db: tuple[str, str], customers: dict[str, uuid.UUID]
) -> None:
    client = _login(app_client)
    for n in range(10):
        _new_ticket(client, f"message {n}")
    response = client.post("/api/v1/tickets", json={"message": "one more"}, headers=_key())
    assert response.status_code == 429
    assert response.json()["code"] == "too_many_requests"
    assert 0 < int(response.headers["Retry-After"]) <= 60
    assert _sql(api_db, "SELECT count(*) FROM tickets") == [(10,)]
    # Another session from the same IP still has its own per-session budget.
    assert (
        _login(app_client, "CUS-0002")
        .post("/api/v1/tickets", json={"message": "hi"}, headers=_key())
        .status_code
        == 200
    )


def test_the_per_ip_message_limit_holds_across_sessions(
    app_client: TestClient,
    app_container: Container,
    api_db: tuple[str, str],
    customers: dict[str, uuid.UUID],
) -> None:
    # The numbers (60 per hour) are checked in tests/unit/test_limits.py; here the wiring: a new
    # session from the same IP does not get a new IP budget (api.md §1, P8-04).
    app_container.rate_limits.messages_per_ip = RateLimiter(3, 60 * 60)
    _new_ticket(_login(app_client, "CUS-0001"), "one")
    _new_ticket(_login(app_client, "CUS-0001"), "two")
    second_customer = _login(app_client, "CUS-0002")
    _new_ticket(second_customer, "three")
    response = second_customer.post("/api/v1/tickets", json={"message": "four"}, headers=_key())
    assert response.status_code == 429
    assert response.json()["code"] == "too_many_requests"
    assert 0 < int(response.headers["Retry-After"]) <= 60 * 60
    assert _sql(api_db, "SELECT count(*) FROM tickets") == [(3,)]


def test_a_replayed_idempotency_key_does_not_use_up_the_limit(
    app_client: TestClient,
    app_container: Container,
    api_db: tuple[str, str],
    customers: dict[str, uuid.UUID],
) -> None:
    """P10-07 (from the P2-06 review): retries after a network drop are free; new messages
    still count."""
    app_container.rate_limits.messages_per_session = RateLimiter(2, 60)
    client = _login(app_client)
    key = _key()
    first = client.post("/api/v1/tickets", json={"message": "Where is my money?"}, headers=key)
    assert first.status_code == 200
    for _ in range(3):
        replay = client.post("/api/v1/tickets", json={"message": "Where is my money?"}, headers=key)
        assert replay.status_code == 200
    assert (
        client.post("/api/v1/tickets", json={"message": "second"}, headers=_key()).status_code
        == 200
    )
    third = client.post("/api/v1/tickets", json={"message": "third"}, headers=_key())
    assert third.status_code == 429
    assert _sql(api_db, "SELECT count(*) FROM tickets") == [(2,)]


def test_4000_escaped_emoji_reach_the_length_check_not_the_body_cap(
    app_client: TestClient, customers: dict[str, uuid.UUID]
) -> None:
    """P10-07 (from the P2-06 review): 12 bytes per JSON-escaped emoji, about 48 KB."""
    client = _login(app_client)
    escaped = "\\ud83d\\ude00"
    for count, expected in ((4_000, 200), (4_001, 422)):
        body = '{"message": "' + escaped * count + '"}'
        assert len(body.encode()) > 32 * 1024
        response = client.post(
            "/api/v1/tickets",
            content=body.encode(),
            headers={**_key(), "Content-Type": "application/json"},
        )
        assert response.status_code == expected, response.text[:200]


def test_placeholder_text_does_not_promise_an_ai_answer() -> None:
    assert "AI" not in tickets_module.PLACEHOLDER_TEXT


# --- failures go to a human ------------------------------------------------------------------


def _break_masking(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ticket handler's mask takes the failure path of the real `mask()`: every digit
    replaced, no counts, `failed=True`. Patched at the handler only: with masking broken
    everywhere, request-id generation in the middleware never ends (reported in the PR)."""

    def failed_mask(text: str) -> MaskResult:
        return MaskResult(text=masking._mask_all_digits(text), counts={}, failed=True)

    monkeypatch.setattr(tickets_module, "mask", failed_mask)


def test_failed_masking_is_audited_and_hands_the_new_ticket_to_a_human(
    app_client: TestClient,
    api_db: tuple[str, str],
    customers: dict[str, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _login(app_client)
    _break_masking(monkeypatch)
    response = client.post("/api/v1/tickets", json={"message": f"card {TEST_CARD}"}, headers=_key())
    assert response.status_code == 200
    events = _events(response)
    assert [name for name, _ in events] == ["ticket.created", "message", "ticket.status", "done"]
    assert events[2][1] == {"status": "handed_off"}
    assert events[0][1]["message"]["masked"] is True  # the customer gets the "we hid" note

    assert _sql(api_db, "SELECT body, masked_entities FROM messages") == [
        ("card #### #### #### ####", {"failed": 1})
    ]
    assert _sql(api_db, "SELECT status, last_route, route_reasons FROM tickets") == [
        ("handed_off", "handoff", ["system_error"])
    ]
    audit = _sql(api_db, "SELECT action, actor_type, details FROM audit_log ORDER BY id")
    assert audit[1:] == [
        ("ticket.created", "customer", {"message_id": audit[1][2]["message_id"]}),
        ("guardrail.masked", "system", {"counts": {}, "failed": True}),
        ("route.decided", "system", {"route": "handoff", "reasons": ["system_error"]}),
    ]


@pytest.mark.parametrize(
    ("status", "last_route", "expected", "handed_off_now"),
    [
        ("answered", "auto_reply", "handed_off", True),  # AI owned: no new run, a human
        ("processing", None, "handed_off", True),
        ("handed_off", "handoff", "handed_off", False),  # already with a human
        ("pending_approval", "approval", "pending_approval", False),
    ],
)
def test_failed_masking_on_a_follow_up_hands_off_unless_a_human_has_it(
    app_client: TestClient,
    api_db: tuple[str, str],
    customers: dict[str, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    last_route: str | None,
    expected: str,
    handed_off_now: bool,
) -> None:
    client = _login(app_client)
    ticket_id = _new_ticket(client)["ticket_id"]
    _sql(
        api_db,
        "UPDATE tickets SET status = %s, last_route = %s WHERE id = %s",
        status,
        last_route,
        ticket_id,
    )
    _break_masking(monkeypatch)
    response = client.post(
        f"/api/v1/tickets/{ticket_id}/messages", json={"message": "pin 1234"}, headers=_key()
    )
    names = [name for name, _ in _events(response)]
    assert ("ticket.status" in names) is handed_off_now
    assert client.get(f"/api/v1/tickets/{ticket_id}").json()["status"] == expected
    failed = _sql(
        api_db,
        "SELECT count(*) FROM audit_log WHERE action = 'guardrail.masked'"
        " AND details->>'failed' = 'true'",
    )
    assert failed == [(1,)]
    decided = _sql(api_db, "SELECT count(*) FROM audit_log WHERE action = 'route.decided'")
    assert decided == [(1 if handed_off_now else 0,)]


def test_a_replay_after_a_failed_mask_carries_the_tickets_status(
    app_client: TestClient,
    api_db: tuple[str, str],
    customers: dict[str, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # P2-06 review (deferred to P6-01): the first response says `handed_off`, so the replay of
    # the same request must too.
    client = _login(app_client)
    _break_masking(monkeypatch)
    key = _key()
    first = _events(client.post("/api/v1/tickets", json={"message": "pin 1234"}, headers=key))
    replay = _events(client.post("/api/v1/tickets", json={"message": "pin 1234"}, headers=key))
    assert [name for name, _ in replay] == [name for name, _ in first]
    assert replay[0] == first[0]
    assert replay[2] == first[2] == ("ticket.status", {"status": "handed_off"})
    ticket_id = first[0][1]["ticket_id"]

    path = f"/api/v1/tickets/{ticket_id}/messages"
    key = _key()
    one = _events(client.post(path, json={"message": "pin 5678"}, headers=key))
    two = _events(client.post(path, json={"message": "pin 5678"}, headers=key))
    assert one[0] == two[0]
    # The first follow-up handed nothing off (a human has the ticket); the replay still says
    # where the ticket is.
    assert "ticket.status" not in [name for name, _ in one]
    assert two[2] == ("ticket.status", {"status": "handed_off"})


def test_a_follow_up_writes_its_audit_records_in_the_same_order_as_a_new_ticket(
    app_client: TestClient,
    api_db: tuple[str, str],
    customers: dict[str, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # P2-06 review (deferred to P6-01): message, then masking, then the route.
    client = _login(app_client)
    ticket_id = _new_ticket(client)["ticket_id"]
    _sql(
        api_db,
        "UPDATE tickets SET status = 'processing', last_route = NULL WHERE id = %s",
        ticket_id,
    )
    before = _sql(api_db, "SELECT max(id) FROM audit_log")[0][0]
    _break_masking(monkeypatch)
    client.post(
        f"/api/v1/tickets/{ticket_id}/messages", json={"message": "pin 1234"}, headers=_key()
    )
    actions = _sql(api_db, "SELECT action FROM audit_log WHERE id > %s ORDER BY id", before)
    assert actions == [("message.received",), ("guardrail.masked",), ("route.decided",)]


def test_a_database_error_puts_no_message_text_in_the_logs(
    capsys: pytest.CaptureFixture[str],
    app_container: Container,
    api_db: tuple[str, str],
    customers: dict[str, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A real database error on the INSERT of the message: the stored body gets a NUL, which
    # Postgres refuses. The error text would carry the bound values without hide_parameters.
    real_add = MessageRepository.add

    async def add_with_nul(self: MessageRepository, **kwargs: Any) -> Message:  # noqa: ANN401
        return await real_add(self, **{**kwargs, "body": kwargs["body"] + "\x00"})

    monkeypatch.setattr(MessageRepository, "add", add_with_nul)
    app = TestClient(
        create_app(app_container), base_url="https://testserver", raise_server_exceptions=False
    )
    client = _login(app)
    private_text = "my landlord Bartholomew owes me rent"
    response = client.post("/api/v1/tickets", json={"message": private_text}, headers=_key())
    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert private_text not in response.text
    logs = capsys.readouterr().out
    assert "request.unhandled_error" in logs
    assert "Bartholomew" not in logs
    assert _sql(api_db, "SELECT count(*) FROM tickets") == [(0,)]


# --- feedback idempotency --------------------------------------------------------------------


def test_feedback_twice_is_stored_once(
    app_client: TestClient, api_db: tuple[str, str], customers: dict[str, uuid.UUID]
) -> None:
    client = _login(app_client)
    ticket_id = _new_ticket(client)["ticket_id"]
    message_id = _assistant_message(api_db, ticket_id)
    path = f"/api/v1/tickets/{ticket_id}/feedback"
    first = client.post(path, json={"message_id": message_id, "rating": "up"})
    second = client.post(path, json={"message_id": message_id, "rating": "down"})
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json() == first.json()
    assert _sql(api_db, "SELECT rating FROM feedback") == [("up",)]
    created = _sql(api_db, "SELECT count(*) FROM audit_log WHERE action = 'feedback.created'")
    assert created == [(1,)]


def test_feedback_comment_with_a_card_is_audited(
    app_client: TestClient, api_db: tuple[str, str], customers: dict[str, uuid.UUID]
) -> None:
    client = _login(app_client)
    ticket_id = _new_ticket(client)["ticket_id"]
    message_id = _assistant_message(api_db, ticket_id)
    response = client.post(
        f"/api/v1/tickets/{ticket_id}/feedback",
        json={"message_id": message_id, "rating": "down", "comment": f"card {TEST_CARD}"},
    )
    feedback_id = response.json()["id"]
    masked = _sql(
        api_db,
        "SELECT entity_type, entity_id, details FROM audit_log WHERE action = 'guardrail.masked'",
    )
    assert masked == [("feedback", feedback_id, {"counts": {"PAN": 1}, "failed": False})]


def test_feedback_without_card_data_writes_no_masking_record(
    app_client: TestClient, api_db: tuple[str, str], customers: dict[str, uuid.UUID]
) -> None:
    client = _login(app_client)
    ticket_id = _new_ticket(client)["ticket_id"]
    message_id = _assistant_message(api_db, ticket_id)
    client.post(
        f"/api/v1/tickets/{ticket_id}/feedback",
        json={"message_id": message_id, "rating": "up", "comment": "very clear, thanks"},
    )
    assert _sql(api_db, "SELECT count(*) FROM audit_log WHERE action = 'guardrail.masked'") == [
        (0,)
    ]


def test_a_database_error_detail_is_logged_as_class_and_constraint_only(
    capsys: pytest.CaptureFixture[str],
    app_container: Container,
    api_db: tuple[str, str],
    customers: dict[str, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A CHECK violation: Postgres answers "Failing row contains (...)" with the row's values,
    # the message body included. hide_parameters does not cover this server detail.
    real_add = MessageRepository.add

    async def add_breaking_a_check(self: MessageRepository, **kwargs: Any) -> Message:  # noqa: ANN401
        # masked_entities must be a JSON object (ck_messages_masked_entities).
        return await real_add(self, **{**kwargs, "masked_entities": ["not an object"]})

    monkeypatch.setattr(MessageRepository, "add", add_breaking_a_check)
    app = TestClient(
        create_app(app_container), base_url="https://testserver", raise_server_exceptions=False
    )
    client = _login(app)
    private_text = "please tell Philippa about the refund"
    response = client.post("/api/v1/tickets", json={"message": private_text}, headers=_key())
    assert response.status_code == 500

    lines = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]
    error = next(line for line in lines if line["event"] == "request.unhandled_error")
    logged = json.dumps(lines)
    assert "Philippa" not in logged
    assert "Failing row" not in logged
    assert "DETAIL" not in logged
    assert error["db_constraint"] == "ck_messages_masked_entities"
    assert error["db_error_class"] == "sqlalchemy.exc.IntegrityError"
    assert [e["exc_type"] for e in error["exception"]] == ["DatabaseErrorSummary"]


# --- one feedback per message, enforced by the database (P10-02) ------------------------------


def test_concurrent_feedback_requests_store_one_rating(
    app_client: TestClient, api_db: tuple[str, str], customers: dict[str, uuid.UUID]
) -> None:
    client = _login(app_client)
    ticket_id = _new_ticket(client)["ticket_id"]
    message_id = _assistant_message(api_db, ticket_id)
    path = f"/api/v1/tickets/{ticket_id}/feedback"
    start = threading.Barrier(2)

    def rate(rating: str) -> Response:
        start.wait()
        return client.post(path, json={"message_id": message_id, "rating": rating})

    with ThreadPoolExecutor(2) as pool:
        answers = list(pool.map(rate, ["up", "down"]))

    assert sorted(a.status_code for a in answers) == [200, 201]
    assert answers[0].json() == answers[1].json()
    assert _sql(api_db, "SELECT count(*) FROM feedback") == [(1,)]
    created = _sql(api_db, "SELECT count(*) FROM audit_log WHERE action = 'feedback.created'")
    assert created == [(1,)]


def test_a_race_past_the_lock_hits_the_unique_constraint_and_answers_the_first_rating(
    app_client: TestClient,
    api_db: tuple[str, str],
    customers: dict[str, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the row lock's check (a second writer), the database refuses the second row."""
    client = _login(app_client)
    ticket_id = _new_ticket(client)["ticket_id"]
    message_id = _assistant_message(api_db, ticket_id)
    path = f"/api/v1/tickets/{ticket_id}/feedback"
    first = client.post(path, json={"message_id": message_id, "rating": "up"})
    real = FeedbackRepository.get_for_message
    calls = {"n": 0}

    async def blind_once(self: FeedbackRepository, mid: uuid.UUID) -> Any:  # noqa: ANN401
        calls["n"] += 1
        return None if calls["n"] == 1 else await real(self, mid)

    monkeypatch.setattr(FeedbackRepository, "get_for_message", blind_once)
    second = client.post(path, json={"message_id": message_id, "rating": "down"})

    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json() == first.json()
    assert _sql(api_db, "SELECT rating FROM feedback") == [("up",)]
    created = _sql(api_db, "SELECT count(*) FROM audit_log WHERE action = 'feedback.created'")
    assert created == [(1,)]


def test_the_database_refuses_a_second_feedback_row_for_a_message(
    app_client: TestClient, api_db: tuple[str, str], customers: dict[str, uuid.UUID]
) -> None:
    client = _login(app_client)
    ticket_id = _new_ticket(client)["ticket_id"]
    message_id = _assistant_message(api_db, ticket_id)
    insert = "INSERT INTO feedback (id, ticket_id, message_id, rating) VALUES (%s, %s, %s, 'up')"
    _sql(api_db, insert, uuid.UUID(str(uuid7())), ticket_id, message_id)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _sql(api_db, insert, uuid.UUID(str(uuid7())), ticket_id, message_id)


# --- the chat's customer picker (P10-02) ------------------------------------------------------


def _demo_client(app_container: Container, tmp_path: Path, entries: object) -> TestClient:
    path = tmp_path / "demo_customers.json"
    path.write_text(json.dumps(entries))
    settings = app_container.settings.model_copy(update={"demo_customers_file": path})
    container = dataclasses.replace(app_container, settings=settings)
    return TestClient(
        create_app(container), base_url="https://testserver", raise_server_exceptions=False
    )


def _card(ref: str) -> dict[str, Any]:
    return {"customer_ref": ref, "hint": f"hint of {ref}", "suggestions": ["Where is my money?"]}


def test_demo_customers_lists_seeded_customers_behind_the_access_code(
    app_container: Container, tmp_path: Path, customers: dict[str, uuid.UUID]
) -> None:
    # CUS-0003 is anonymised (erased) and CUS-0099 does not exist: both are left out.
    entries = [_card("CUS-0002"), _card("CUS-0003"), _card("CUS-0099"), _card("CUS-0001")]
    with _demo_client(app_container, tmp_path, entries) as client:
        response = client.get("/api/v1/demo/customers", headers={"X-Demo-Access-Code": ACCESS_CODE})

    assert response.status_code == 200, response.text
    assert response.json() == [
        {
            "customer_ref": ref,
            "display_name": f"Customer {ref}",
            "tier": "verified",
            "hint": f"hint of {ref}",
            "suggestions": ["Where is my money?"],
        }
        for ref in ("CUS-0002", "CUS-0001")
    ]


def test_demo_customers_needs_the_right_code_and_limits_wrong_ones(
    app_container: Container, tmp_path: Path, customers: dict[str, uuid.UUID]
) -> None:
    with _demo_client(app_container, tmp_path, [_card("CUS-0001")]) as client:
        assert client.get("/api/v1/demo/customers").status_code == 422
        statuses = [
            client.get(
                "/api/v1/demo/customers", headers={"X-Demo-Access-Code": "wrong"}
            ).status_code
            for _ in range(6)
        ]
        # The limit is shared with POST /demo/session: the right code is refused now too.
        blocked = client.get("/api/v1/demo/customers", headers={"X-Demo-Access-Code": ACCESS_CODE})

    assert statuses == [401] * 5 + [429]
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers


@pytest.mark.parametrize("entries", [{"not": "a list"}, [{"customer_ref": "CUS-0001"}]])
def test_a_broken_demo_customers_file_is_503(
    app_container: Container, tmp_path: Path, customers: dict[str, uuid.UUID], entries: object
) -> None:
    with _demo_client(app_container, tmp_path, entries) as client:
        response = client.get("/api/v1/demo/customers", headers={"X-Demo-Access-Code": ACCESS_CODE})

    assert (response.status_code, response.json()["code"]) == (503, "demo_unavailable")


def test_the_committed_demo_customers_file_is_valid() -> None:
    """The file the deployment serves parses (data-check checks it against the seed too)."""
    from hub.api.demo import load_demo_customers

    entries = load_demo_customers(Path(__file__).parents[4] / "data/seed/demo_customers.json")
    assert len(entries) == 6
