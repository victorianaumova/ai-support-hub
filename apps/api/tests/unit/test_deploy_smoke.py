"""The post-deploy canary (infra/deploy/smoke.py, P12): it asks as a customer that cannot be a
repeat contact, reads the stream in any field order and accepts only the auto_reply and approval
routes."""

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from types import ModuleType

import pytest

from ._llm import ROOT


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("deploy_smoke", ROOT / "infra/deploy/smoke.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = _load()
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


def _ticket(status: str, age: timedelta) -> dict[str, str]:
    return {"status": status, "created_at": (NOW - age).isoformat()}


@pytest.mark.parametrize(
    ("tickets", "clean"),
    [
        ([], True),
        ([_ticket("closed", timedelta(days=18))], True),
        # closed, but its memory item may still be in the 14-day window
        ([_ticket("closed", timedelta(days=10))], False),
        # open, however old: an open ticket on the same intent is a repeat contact
        ([_ticket("answered", timedelta(days=30))], False),
        ([_ticket("pending_approval", timedelta(days=30))], False),
        ([_ticket("closed", timedelta(days=40)), _ticket("handed_off", timedelta(days=1))], False),
    ],
)
def test_a_customer_is_clean_without_an_open_or_recent_ticket(
    tickets: list[dict[str, str]], clean: bool
) -> None:
    assert smoke.is_clean(tickets, NOW) is clean


def _lines(*events: tuple[str, object]) -> list[str]:
    lines: list[str] = [": ping", ""]
    for name, data in events:
        lines += [f"data: {json.dumps(data)}", f"event: {name}", ""]
    return lines


def test_the_stream_is_read_whatever_the_order_of_its_fields() -> None:
    lines = _lines(("ticket.created", {"ticket_id": "t1"}), ("done", {}))
    assert smoke.stream_events(lines) == [("ticket.created", '{"ticket_id": "t1"}'), ("done", "{}")]


@pytest.mark.parametrize("status", ["answered", "pending_approval"])
def test_the_canary_accepts_auto_reply_and_approval(status: str) -> None:
    events = smoke.stream_events(
        _lines(("message", {}), ("ticket.status", {"status": status}), ("done", {}))
    )
    assert smoke.check_stream(events) == status


@pytest.mark.parametrize(
    ("events", "why"),
    [
        ((("ticket.status", {"status": "handed_off"}), ("done", {})), "handed_off"),
        ((("error", {}), ("ticket.status", {"status": "answered"}), ("done", {})), "error"),
        ((("ticket.status", {"status": "answered"}),), "did not end"),
        ((("message", {}), ("done", {})), "did not end"),
    ],
)
def test_the_canary_fails_on_a_handoff_an_error_or_a_cut_stream(
    events: tuple[tuple[str, object], ...], why: str
) -> None:
    with pytest.raises(SystemExit, match=why):
        smoke.check_stream(smoke.stream_events(_lines(*events)))


class _Response:
    def __init__(self, body: object = None, cookies: dict[str, str] | None = None) -> None:
        self._body = body
        self.cookies = cookies or {}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._body


class _Client:
    """The two demo endpoints and the ticket list, with no network: `tickets` per customer."""

    def __init__(self, listed: list[str], tickets: dict[str, list[dict[str, str]]]) -> None:
        self.listed = listed
        self.tickets = tickets
        self.logins: list[str] = []

    def post(self, path: str, json: dict[str, str]) -> _Response:
        assert path == "/api/v1/demo/session"
        self.logins.append(json["customer_ref"])
        return _Response(cookies={"hub_customer": json["customer_ref"]})

    def get(self, path: str, headers: dict[str, str], params: object = None) -> _Response:
        if path == "/api/v1/demo/customers":
            return _Response([{"customer_ref": ref} for ref in self.listed])
        ref = headers["Cookie"].removeprefix("hub_customer=")
        return _Response({"items": self.tickets.get(ref, []), "next_cursor": None})


def test_the_canary_never_asks_as_a_restricted_customer() -> None:
    # Deploy #17: CUS-0007 (restricted) came first and the canary was handed off.
    client = _Client(["CUS-0007", "CUS-0001", "CUS-0016"], {})
    ref, cookie = smoke.clean_customer(client, "code")
    assert ref == "CUS-0001"
    assert cookie == {"Cookie": "hub_customer=CUS-0001"}
    assert "CUS-0007" not in client.logins


def test_the_canary_takes_the_next_clean_allowed_customer() -> None:
    recent = [_ticket("closed", timedelta(days=1))]
    client = _Client(["CUS-0007", "CUS-0001", "CUS-0016"], {"CUS-0001": recent})
    assert smoke.clean_customer(client, "code")[0] == "CUS-0016"


def test_the_canary_fails_when_only_restricted_customers_are_listed() -> None:
    with pytest.raises(SystemExit, match="non-restricted"):
        smoke.clean_customer(_Client(["CUS-0007"], {}), "code")


def test_the_canary_customers_are_demo_customers_and_not_restricted() -> None:
    demo = {entry["customer_ref"] for entry in json.loads(
        (ROOT / "data/seed/demo_customers.json").read_text())}  # fmt: skip
    customers = json.loads((ROOT / "data/seed/customers.json").read_text())
    restricted = {c["external_ref"] for c in customers if c["restricted"]}
    assert set(smoke.CANARY_CUSTOMERS) <= demo
    assert not set(smoke.CANARY_CUSTOMERS) & restricted
