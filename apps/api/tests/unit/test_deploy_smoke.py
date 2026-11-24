"""The post-deploy canary (infra/deploy/smoke.py, P12): it asks as one fixed, non-restricted demo
customer, reads the stream in any field order, accepts only the auto_reply and approval routes and
closes its own ticket afterwards (best effort)."""

import importlib.util
import json
from collections.abc import Iterator
from contextlib import contextmanager
from types import ModuleType
from typing import Any

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
    def __init__(
        self, body: object = None, cookies: dict[str, str] | None = None, status: int = 200
    ) -> None:
        self._body = body
        self.cookies = cookies or {}
        self.status_code = status
        self.text = json.dumps(body)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise smoke.httpx2.HTTPStatusError("failed", request=None, response=None)

    def json(self) -> object:
        return self._body

    def iter_lines(self) -> Iterator[str]:
        yield from self._body  # type: ignore[misc]


class _Demo:
    """The demo API with no network: one canary stream, then the console's ticket that moves
    through `states` (status, has a draft); each reject or close moves it to the next state."""

    def __init__(
        self,
        route_status: str = "answered",
        states: list[str] | None = None,
        login_status: int = 200,
        stale_first_close: bool = False,
    ) -> None:
        self.route_status = route_status
        self.states = states if states is not None else ["answered", "closed"]
        self.login_status = login_status
        self.stale_first_close = stale_first_close
        self.calls: list[str] = []
        self.logins: list[str] = []
        self.version = 1

    def post(
        self, path: str, json: dict[str, Any], headers: dict[str, str] | None = None
    ) -> _Response:
        self.calls.append(path)
        if path == "/api/v1/demo/session":
            self.logins.append(json["customer_ref"])
            return _Response(cookies={"hub_customer": json["customer_ref"]})
        if path == "/api/v1/auth/login":
            assert json["email"] == smoke.CANARY_OPERATOR
            return _Response({"csrf_token": "csrf"}, {"hub_operator": "op"}, self.login_status)
        assert headers is not None
        assert headers["X-CSRF-Token"] == "csrf"
        assert json["ticket_version"] == self.version
        if path.endswith("/close") and self.stale_first_close:
            self.stale_first_close = False
            self.version += 1
            return _Response({"code": "stale_draft"}, status=409)
        if path.endswith("/reject"):
            assert path == "/api/v1/console/drafts/d1/reject"
            assert "Idempotency-Key" in headers
        self.states.pop(0)
        self.version += 1
        return _Response({})

    def get(self, path: str, headers: dict[str, str]) -> _Response:
        self.calls.append(path)
        assert path == "/api/v1/console/tickets/t1"
        status = self.states[0]
        if status == "processing":
            self.states.pop(0)
        draft = {"id": "d1"} if status == "pending_approval" else None
        return _Response({"status": status, "version": self.version, "draft": draft})

    @contextmanager
    def stream(
        self, method: str, path: str, json: object, headers: dict[str, str]
    ) -> Iterator[_Response]:
        assert headers["Cookie"] == f"hub_customer={smoke.CANARY_CUSTOMER}"
        yield _Response(
            _lines(
                ("ticket.created", {"ticket_id": "t1", "message": {}}),
                ("message", {}),
                ("ticket.status", {"status": self.route_status}),
                ("done", {}),
            )
        )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(smoke.time, "sleep", lambda _: None)


def test_the_canary_asks_as_the_fixed_customer_and_closes_its_answered_ticket() -> None:
    demo = _Demo()
    smoke.canary(demo, "code", "pw")
    assert demo.logins == [smoke.CANARY_CUSTOMER]
    assert demo.calls[-1] == "/api/v1/console/tickets/t1/close"
    assert demo.states == ["closed"]


def test_a_draft_waiting_for_approval_is_rejected_without_a_reply_then_the_ticket_is_closed() -> (
    None
):
    demo = _Demo("pending_approval", ["pending_approval", "handed_off", "closed"])
    smoke.canary(demo, "code", "pw")
    posts = [c for c in demo.calls if c.startswith("/api/v1/console/") and not c.endswith("t1")]
    assert posts == ["/api/v1/console/drafts/d1/reject", "/api/v1/console/tickets/t1/close"]
    assert demo.states == ["closed"]


def test_a_run_still_finishing_and_a_stale_version_are_read_again() -> None:
    demo = _Demo(states=["processing", "answered", "closed"], stale_first_close=True)
    smoke.canary(demo, "code", "pw")
    assert demo.calls.count("/api/v1/console/tickets/t1/close") == 2
    assert demo.states == ["closed"]


def test_a_failed_close_is_a_warning_and_not_a_failed_check(
    capsys: pytest.CaptureFixture[str],
) -> None:
    demo = _Demo(login_status=401)
    smoke.canary(demo, "code", "pw")
    assert "::warning::the canary ticket t1 was not closed" in capsys.readouterr().out
    assert "/api/v1/console/tickets/t1/close" not in demo.calls


def test_a_ticket_that_never_closes_is_a_warning(capsys: pytest.CaptureFixture[str]) -> None:
    demo = _Demo(states=["processing"] * (smoke.CLOSE_ATTEMPTS + 5))
    smoke.canary(demo, "code", "pw")
    assert "could not be closed" in capsys.readouterr().out


def test_a_handoff_fails_the_canary_before_any_close() -> None:
    demo = _Demo("handed_off")
    with pytest.raises(SystemExit, match="handed_off"):
        smoke.canary(demo, "code", "pw")
    assert not [c for c in demo.calls if c.startswith("/api/v1/console/")]


def test_the_canary_customer_is_a_demo_customer_and_not_restricted() -> None:
    # Deploy #17: the canary asked as CUS-0007 (restricted) and was handed off.
    demo = {entry["customer_ref"] for entry in json.loads(
        (ROOT / "data/seed/demo_customers.json").read_text())}  # fmt: skip
    customers = json.loads((ROOT / "data/seed/customers.json").read_text())
    restricted = {c["external_ref"] for c in customers if c["restricted"]}
    assert smoke.CANARY_CUSTOMER in demo
    assert smoke.CANARY_CUSTOMER not in restricted


def test_the_canary_operator_is_the_demo_admin() -> None:
    operators = json.loads((ROOT / "data/seed/operators.json").read_text())
    assert {o["email"]: o["role"] for o in operators}[smoke.CANARY_OPERATOR] == "admin"
