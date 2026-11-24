"""Customer endpoints without a database: authorisation, input limits, schema of the views.

The database paths are in tests/integration/test_tickets_api.py.
"""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from hub.api import tickets
from hub.api.dependencies import current_customer, db_session
from hub.api.limits import MAX_BODY_BYTES
from hub.api.schemas import MessageIn, MessageOut, TicketOut, citations_for_customer
from hub.db.models import Customer
from hub.db.session import connect
from hub.domain.enums import CustomerTier, TicketStatus

from .conftest import MakeClient

TICKET = uuid.uuid4()
KEY = {"Idempotency-Key": str(uuid.uuid4())}

CUSTOMER_ROUTES = [
    ("POST", "/api/v1/tickets", {"message": "hi"}),
    ("POST", f"/api/v1/tickets/{TICKET}/messages", {"message": "hi"}),
    ("GET", "/api/v1/tickets", None),
    ("GET", f"/api/v1/tickets/{TICKET}", None),
    ("GET", f"/api/v1/tickets/{TICKET}/messages", None),
    ("POST", f"/api/v1/tickets/{TICKET}/request-human", None),
    ("POST", f"/api/v1/tickets/{TICKET}/feedback", {"message_id": str(TICKET), "rating": "up"}),
]


def _client_with_customer(make_client: MakeClient) -> TestClient:
    client = make_client()
    customer = Customer(
        id=uuid.uuid4(),
        external_ref="CUS-0001",
        display_name="Test",
        email="t@example.com",
        tier=CustomerTier.VERIFIED,
        country="DE",
    )
    overrides = client.app.dependency_overrides  # type: ignore[attr-defined]
    overrides[current_customer] = lambda: customer
    # Never used: these tests fail validation before the handler runs.
    overrides[db_session] = lambda: None
    return client


@pytest.mark.parametrize(("method", "path", "body"), CUSTOMER_ROUTES)
def test_every_customer_route_needs_a_session(
    make_client: MakeClient, method: str, path: str, body: dict[str, str] | None
) -> None:
    response = make_client().request(method, path, json=body, headers=KEY)
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_every_customer_route_is_covered_by_the_authorisation_test(make_client: MakeClient) -> None:
    # From the OpenAPI document: included routers are not plain entries of `app.routes`.
    paths = make_client().app.openapi()["paths"]  # type: ignore[attr-defined]
    routes = {
        (method.upper(), path)
        for path, operations in paths.items()
        if path.startswith("/api/v1/tickets")
        for method in operations
    }
    tested = {
        (method, path.replace(str(TICKET), "{ticket_id}")) for method, path, _ in CUSTOMER_ROUTES
    }
    assert routes == tested


def test_a_forged_cookie_is_refused(make_client: MakeClient) -> None:
    client = make_client()
    client.cookies.set("hub_customer", "forged.value.here")
    assert client.get("/api/v1/tickets").status_code == 401


def test_message_over_4000_characters_is_refused_before_masking(
    make_client: MakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def must_not_run(_text: str) -> None:
        raise AssertionError("mask() ran on an over-long message")

    monkeypatch.setattr(tickets, "mask", must_not_run)
    client = _client_with_customer(make_client)
    for path in ("/api/v1/tickets", f"/api/v1/tickets/{TICKET}/messages"):
        response = client.post(path, json={"message": "4" * 4001}, headers=KEY)
        assert response.status_code == 422
        assert response.headers["content-type"] == "application/problem+json"
        problem = response.json()
        assert problem["code"] == "validation_error"
        assert problem["errors"][0]["loc"] == ["body", "message"]
        assert "4444" not in response.text  # the input is never echoed


@pytest.mark.parametrize("message", ["", "   \n\t"])
def test_empty_message_is_refused(make_client: MakeClient, message: str) -> None:
    response = _client_with_customer(make_client).post(
        "/api/v1/tickets", json={"message": message}, headers=KEY
    )
    assert response.status_code == 422


def test_unknown_fields_are_refused(make_client: MakeClient) -> None:
    # The customer identity comes from the session; a body field cannot set it.
    response = _client_with_customer(make_client).post(
        "/api/v1/tickets", json={"message": "hi", "customer_ref": "CUS-0002"}, headers=KEY
    )
    assert response.status_code == 422


@pytest.mark.parametrize("headers", [{}, {"Idempotency-Key": "not-a-uuid"}])
def test_idempotency_key_must_be_a_uuid(make_client: MakeClient, headers: dict[str, str]) -> None:
    response = _client_with_customer(make_client).post(
        "/api/v1/tickets", json={"message": "hi"}, headers=headers
    )
    assert response.status_code == 422


def test_huge_body_is_refused_with_413_and_a_request_id(make_client: MakeClient) -> None:
    response = make_client().post(
        "/api/v1/tickets", content=b'{"message": "' + b"4" * MAX_BODY_BYTES + b'"}', headers=KEY
    )
    assert response.status_code == 413
    assert response.json()["code"] == "content_too_large"
    assert response.headers["X-Request-ID"]


def test_demo_session_is_unavailable_without_secret_and_code(make_client: MakeClient) -> None:
    response = make_client().post(
        "/api/v1/demo/session", json={"customer_ref": "CUS-0001", "access_code": "x"}
    )
    assert response.status_code == 503
    assert response.json()["code"] == "demo_unavailable"


def test_customer_views_have_no_internal_fields() -> None:
    internal = {"intent", "confidence", "route_reasons", "last_route", "summary", "chunk_id"}
    assert not internal & set(TicketOut.model_fields)
    assert not internal & set(MessageOut.model_fields)


def test_ticket_view_carries_the_ai_disclosure() -> None:
    now = datetime.now(UTC)
    view = TicketOut(
        id=uuid.uuid4(), status=TicketStatus.PROCESSING, ai_disclosed_at=now, created_at=now
    )
    assert view.ai_disclosure.startswith("You are chatting with Tallybridge's AI assistant.")


def test_citations_drop_internal_ids() -> None:
    stored = [{"marker": 1, "chunk_id": "a#1", "doc_id": "a", "title": "T", "url": "/help/a"}, "x"]
    assert [c.model_dump() for c in citations_for_customer(stored)] == [
        {"marker": 1, "title": "T", "url": "/help/a"}
    ]


@pytest.mark.parametrize(
    "url",
    [
        "/help/withdrawal-times",
        "/help/withdrawal-times#sepa-withdrawals",
        "/help/error-codes#err-2003",
        "/help/fees#fees-1",
        "/help/cards#überweisung",  # anchors keep word characters of any script (kb/sections)
        "",  # an account fact: no help-centre page
    ],
)
def test_server_built_help_links_reach_the_customer(url: str) -> None:
    stored = [{"marker": 1, "title": "T", "url": url}]
    assert citations_for_customer(stored)[0].url == url


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "https://evil.example/help/a",
        "//evil.example/help/a",
        "/help/../admin",
        "/help/a?next=https://evil.example",
        "/help/a#x y",
        "/help/A",
        "/help/a\n",
        "/console/tickets",
        None,
        42,
    ],
)
def test_any_other_citation_url_reaches_the_customer_as_no_link(url: object) -> None:
    # T13 (P8-04): links only from citations the server built; anything else shows no link.
    stored = [{"marker": 1, "title": "T", "url": url}]
    assert citations_for_customer(stored)[0].url == ""


@pytest.mark.parametrize("message", ["a\x00b", "\x00"])
def test_nul_in_a_message_is_refused(make_client: MakeClient, message: str) -> None:
    # Postgres text cannot hold NUL; without this the INSERT fails with a 500.
    response = _client_with_customer(make_client).post(
        "/api/v1/tickets", json={"message": message}, headers=KEY
    )
    assert response.status_code == 422


def test_nul_in_a_feedback_comment_is_refused(make_client: MakeClient) -> None:
    response = _client_with_customer(make_client).post(
        f"/api/v1/tickets/{TICKET}/feedback",
        json={"message_id": str(TICKET), "rating": "up", "comment": "bad\x00"},
    )
    assert response.status_code == 422


def test_exactly_4000_characters_pass_the_schema() -> None:
    assert MessageIn(message="x" * 4000).message == "x" * 4000


def test_database_errors_hide_the_bound_values() -> None:
    # Customer text is a bound value; it must not appear in the text of a database error.
    database = connect("postgresql+psycopg://hub:pw@db/hub")
    assert database.engine.sync_engine.hide_parameters is True
