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
