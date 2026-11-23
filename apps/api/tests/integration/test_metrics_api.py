"""Metrics, evaluation summary, settings and audit endpoints against Postgres (P10-09).

Done when: "Numbers match SQL checks on seeded data." The seeded rows below are small enough to
count by hand; every expected number is written out with how it was reached, and the totals are
checked once more with plain SQL of their own.
"""

import dataclasses
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from uuid_utils.compat import uuid7

from hub.deps import Container
from hub.main import create_app

from . import _api
from ._api import Urls, add_customer, add_operator, run_sql

DATABASE = "hub_metrics_api_test"
EMAILS = {
    "operator": "marta@tallybridge.test",
    "lead": "lena@tallybridge.test",
    "compliance": "olga@tallybridge.test",
}
PERIOD = {"from": "2026-11-10", "to": "2026-11-12"}


def _id() -> uuid.UUID:
    return uuid.UUID(str(uuid7()))


def _at(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 11, day, hour, tzinfo=UTC)


@pytest.fixture(scope="module")
def urls(postgres_url: str) -> Iterator[Urls]:
    with _api.own_database(postgres_url, DATABASE) as found:
        yield found


@pytest.fixture
def runs_dir(tmp_path: Path) -> Path:
    return tmp_path / "runs"


@pytest.fixture
def app(urls: Urls, runs_dir: Path) -> Iterator[FastAPI]:
    _api.empty_tables(urls)
    run_sql(urls, "DELETE FROM settings")
    for who, email in EMAILS.items():
        add_operator(urls, email, who, name=who.title())
    base = _api.container(urls)
    container = dataclasses.replace(
        base, settings=base.settings.model_copy(update={"eval_runs_dir": runs_dir})
    )
    assert isinstance(container, Container)
    application = create_app(container)
    with TestClient(application):
        yield application


@pytest.fixture
def staff(app: FastAPI) -> dict[str, TestClient]:
    return {who: _api.operator_client(app, email) for who, email in EMAILS.items()}


# --- seeded data -----------------------------------------------------------------------------


class Seed:
    def __init__(self, urls: Urls) -> None:
        self.urls = urls
        self.customer = add_customer(urls)

    def ticket(
        self, day: int, status: str = "answered", first_response_s: int | None = None
    ) -> uuid.UUID:
        ticket_id = _id()
        created = _at(day)
        first = None if first_response_s is None else created + timedelta(seconds=first_response_s)
        run_sql(
            self.urls,
            "INSERT INTO tickets (id, customer_id, channel, status, ai_disclosed_at,"
            " first_response_at, created_at) VALUES (%s, %s, 'chat', %s, %s, %s, %s)",
            ticket_id,
            self.customer,
            status,
            created,
            first,
            created,
        )
        return ticket_id

    def run(
        self,
        ticket_id: uuid.UUID,
        route: str,
        reasons: list[str],
        latency_ms: int,
        minutes: int = 0,
    ) -> uuid.UUID:
        run_id = _id()
        started = run_sql(self.urls, "SELECT created_at FROM tickets WHERE id = %s", ticket_id)[0][
            0
        ] + timedelta(minutes=minutes)
        run_sql(
            self.urls,
            "INSERT INTO runs (id, ticket_id, status, route, route_reasons, graph_version,"
            " latency_ms, started_at, finished_at)"
            " VALUES (%s, %s, 'completed', %s, %s::jsonb, 'graph-test', %s, %s, %s)",
            run_id,
            ticket_id,
            route,
            json.dumps(reasons),
            latency_ms,
            started,
            started,
        )
        return run_id

    def draft(
        self, ticket_id: uuid.UUID, run_id: uuid.UUID, status: str, edit_ratio: float | None
    ) -> None:
        run_sql(
            self.urls,
            "INSERT INTO drafts (id, run_id, ticket_id, can_answer, body, status, edit_ratio)"
            " VALUES (%s, %s, %s, true, 'Draft text.', %s, %s)",
            _id(),
            run_id,
            ticket_id,
            status,
            edit_ratio,
        )

    def message(self, ticket_id: uuid.UUID, role: str) -> uuid.UUID:
        message_id = _id()
        run_sql(
            self.urls,
            "INSERT INTO messages (id, ticket_id, role, body) VALUES (%s, %s, %s, 'Text.')",
            message_id,
            ticket_id,
            role,
        )
        return message_id

    def feedback(self, ticket_id: uuid.UUID, rating: str) -> None:
        message_id = self.message(ticket_id, "assistant")
        run_sql(
            self.urls,
            "INSERT INTO feedback (id, ticket_id, message_id, rating) VALUES (%s, %s, %s, %s)",
            _id(),
            ticket_id,
            message_id,
            rating,
        )

    def call(
        self, at: datetime, node: str, cost: str, mode: str = "real", context: str = "online"
    ) -> None:
        run_sql(
            self.urls,
            "INSERT INTO llm_calls (id, context, mode, node, model, input_tokens, output_tokens,"
            " cached_tokens, cost_usd, latency_ms, status, created_at)"
            " VALUES (%s, %s, %s, %s, 'test-model', 1, 1, 0, %s, 1, 'ok', %s)",
            _id(),
            context,
            mode,
            node,
            cost,
            at,
        )


@pytest.fixture
def seeded(app: FastAPI, urls: Urls) -> Seed:
    seed = Seed(urls)
    # T1: auto-reply, closed, liked: the only AI-resolved ticket.
    t1 = seed.ticket(10, "closed", first_response_s=30)
    seed.run(t1, "auto_reply", [], 4000)
    seed.feedback(t1, "up")
    # T2: auto-reply, closed, but "not helpful": not AI-resolved.
    t2 = seed.ticket(10, "closed")
    seed.run(t2, "auto_reply", [], 6000)
    seed.feedback(t2, "down")
    # T3: approval, approved unchanged.
    t3 = seed.ticket(11, first_response_s=60)
    seed.draft(t3, seed.run(t3, "approval", ["low_confidence"], 8000), "approved", 0)
    # T4: handoff; its later approval run does not change the ticket's route or reasons.
    t4 = seed.ticket(11, "handed_off")
    seed.run(t4, "handoff", ["fraud_report", "policy_intent"], 2000)
    seed.run(t4, "approval", ["low_confidence"], 3000, minutes=5)
    # T5: approval, edited (0.2).
    t5 = seed.ticket(12)
    seed.draft(t5, seed.run(t5, "approval", ["low_confidence"], 10000), "edited", 0.2)
    # T6: approval; the first draft rejected, the second (later run) edited (0.4).
    t6 = seed.ticket(12)
    seed.draft(t6, seed.run(t6, "approval", ["auto_reply_off"], 5000), "rejected", None)
    seed.draft(t6, seed.run(t6, "approval", ["low_confidence"], 7000, minutes=5), "edited", 0.4)
    # T7: no run yet.
    seed.ticket(12, "processing")
    # T8: the day before the period: counts nowhere.
    t8 = seed.ticket(9, "closed")
    seed.run(t8, "auto_reply", [], 1000)
    seed.feedback(t8, "up")
    # Cost: real online and memory calls in the period count; fake, `record` and calls outside
    # the period do not.
    seed.call(_at(10), "triage", "0.001")
    seed.call(_at(10), "write", "0.002")
    seed.call(_at(12), "write", "0.003")
    seed.call(_at(11), "memory_summary", "0.0005", context="memory")
    seed.call(_at(11), "write", "0.5", mode="fake")
    seed.call(_at(11), "write", "0.7", context="record")
    seed.call(_at(9), "write", "0.9")
    # Today's spend for the budget box.
    seed.call(datetime.now(UTC), "triage", "0.004")
    run_sql(urls, "INSERT INTO settings (key, value) VALUES ('daily_budget_usd', '0.2')")
    return seed


# --- summary ---------------------------------------------------------------------------------


def test_the_summary_matches_the_seeded_rows(staff: dict[str, TestClient], seeded: Seed) -> None:
    response = staff["lead"].get("/api/v1/console/metrics/summary", params=PERIOD)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "period": {"from": "2026-11-10", "to": "2026-11-12"},
        # T1-T7; first routes: T1, T2 auto; T3, T5, T6 approval; T4 handoff; T7 none.
        "tickets": {"total": 7, "auto_reply": 2, "approval": 3, "handoff": 1},
        "automation_share": round(2 / 7, 4),
        "ai_resolved_share": round(1 / 7, 4),
        # First routed runs only: T4's and T6's later low_confidence runs are not counted.
        "route_reasons": [
            {"reason": "low_confidence", "count": 2},
            {"reason": "auto_reply_off", "count": 1},
            {"reason": "fraud_report", "count": 1},
            {"reason": "policy_intent", "count": 1},
        ],
        # Every reviewed draft of the period's tickets; the median of the edited ones (0.2, 0.4).
        "drafts": {
            "approved": 1,
            "edited": 2,
            "rejected": 1,
            "approved_unchanged_share": 0.25,
            "median_edit_ratio": 0.3,
        },
        # 30 s and 60 s: median 45 s; p95 = 30 + 0.95 x 30 s (linear interpolation).
        "first_response_ms": {"p50": 45000, "p95": 58500},
        # Every run of the period's tickets: 2,3,4,5,6,7,8,10 s; p95 = 8 + 0.65 x 2 s.
        "latency_ms": {"p50": 5500, "p95": 9300},
        "cost_usd": {
            "total": 0.0065,
            "per_ticket": round(0.0065 / 7, 6),
            "by_node": {"memory_summary": 0.0005, "triage": 0.001, "write": 0.005},
        },
        "feedback": {"up": 1, "down": 1},
        "budget": {"daily_limit_usd": 0.2, "spent_today_usd": 0.004},
    }


def test_the_summary_totals_agree_with_plain_sql(
    staff: dict[str, TestClient], seeded: Seed, urls: Urls
) -> None:
    body = staff["lead"].get("/api/v1/console/metrics/summary", params=PERIOD).json()
    lower, upper = _at(10, 0), _at(13, 0)

    [(tickets,)] = run_sql(
        urls,
        "SELECT count(*) FROM tickets WHERE created_at >= %s AND created_at < %s",
        lower,
        upper,
    )
    [(cost,)] = run_sql(
        urls,
        "SELECT sum(cost_usd) FROM llm_calls WHERE mode = 'real' AND context <> 'record'"
        " AND created_at >= %s AND created_at < %s",
        lower,
        upper,
    )
    [(up, down)] = run_sql(
        urls,
        "SELECT count(*) FILTER (WHERE rating = 'up'), count(*) FILTER (WHERE rating = 'down')"
        " FROM feedback f JOIN tickets t ON t.id = f.ticket_id"
        " WHERE t.created_at >= %s AND t.created_at < %s",
        lower,
        upper,
    )

    assert body["tickets"]["total"] == tickets
    assert body["cost_usd"]["total"] == float(cost)
    assert (body["feedback"]["up"], body["feedback"]["down"]) == (up, down)


def test_an_empty_period_has_no_shares_instead_of_zero(
    staff: dict[str, TestClient], seeded: Seed
) -> None:
    body = (
        staff["lead"]
        .get("/api/v1/console/metrics/summary", params={"from": "2026-10-01", "to": "2026-10-02"})
        .json()
    )

    assert body["tickets"] == {"total": 0, "auto_reply": 0, "approval": 0, "handoff": 0}
    assert body["automation_share"] is None
    assert body["drafts"]["approved_unchanged_share"] is None
    assert body["latency_ms"] == {"p50": None, "p95": None}
    assert body["cost_usd"]["per_ticket"] is None


@pytest.mark.parametrize(
    "params",
    [{"from": "2026-11-12", "to": "2026-11-10"}, {"from": "2025-01-01", "to": "2026-11-12"}],
)
def test_a_reversed_or_too_long_period_is_a_validation_error(
    staff: dict[str, TestClient], params: dict[str, str]
) -> None:
    response = staff["lead"].get("/api/v1/console/metrics/summary", params=params)

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


# --- timeseries ------------------------------------------------------------------------------


def test_routes_per_day_fill_empty_days(staff: dict[str, TestClient], seeded: Seed) -> None:
    response = staff["lead"].get(
        "/api/v1/console/metrics/timeseries",
        params={"metric": "routes", "from": "2026-11-09", "to": "2026-11-13"},
    )

    assert response.status_code == 200, response.text
    assert [(p["date"], p["values"]) for p in response.json()["points"]] == [
        ("2026-11-09", {"auto_reply": 1, "approval": 0, "handoff": 0}),
        ("2026-11-10", {"auto_reply": 2, "approval": 0, "handoff": 0}),
        ("2026-11-11", {"auto_reply": 0, "approval": 1, "handoff": 1}),
        ("2026-11-12", {"auto_reply": 0, "approval": 2, "handoff": 0}),
        ("2026-11-13", {"auto_reply": 0, "approval": 0, "handoff": 0}),
    ]


@pytest.mark.parametrize(
    ("metric", "expected"),
    [
        ("tickets", [{"total": 2}, {"total": 2}, {"total": 3}]),
        (
            "latency",
            [{"p50": 5000, "p95": 5900}, {"p50": 3000, "p95": 7500}, {"p50": 7000, "p95": 9700}],
        ),
        ("cost", [{"total_usd": 0.003}, {"total_usd": 0.0005}, {"total_usd": 0.003}]),
    ],
)
def test_each_timeseries_metric_per_day(
    staff: dict[str, TestClient], seeded: Seed, metric: str, expected: list[dict[str, Any]]
) -> None:
    response = staff["lead"].get(
        "/api/v1/console/metrics/timeseries", params={"metric": metric, **PERIOD}
    )

    assert response.status_code == 200, response.text
    assert [p["values"] for p in response.json()["points"]] == expected


def test_an_unknown_metric_or_bucket_is_refused(staff: dict[str, TestClient]) -> None:
    lead = staff["lead"]
    path = "/api/v1/console/metrics/timeseries"
    assert lead.get(path, params={"metric": "secrets"}).status_code == 422
    assert lead.get(path, params={"metric": "routes", "bucket": "hour"}).status_code == 422


# --- roles -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        ("/api/v1/console/metrics/summary", {"lead"}),
        ("/api/v1/console/metrics/timeseries?metric=tickets", {"lead"}),
        ("/api/v1/console/evals/latest", {"lead"}),
        ("/api/v1/admin/settings", {"lead"}),
        ("/api/v1/console/audit", {"lead", "compliance"}),
        ("/api/v1/console/audit/verify", {"lead", "compliance"}),
    ],
)
def test_who_may_read_what(staff: dict[str, TestClient], path: str, allowed: set[str]) -> None:
    for who, client in staff.items():
        status = client.get(path).status_code
        assert status == (200 if who in allowed else 403), (who, path, status)


def test_without_a_session_every_read_is_unauthorized(app: FastAPI) -> None:
    anonymous = _api.client(app)
    assert anonymous.get("/api/v1/console/metrics/summary").status_code == 401
    assert anonymous.get("/api/v1/admin/settings").status_code == 401


# --- evaluation summary ----------------------------------------------------------------------


def _eval_run(
    runs_dir: Path, name: str, *, mode: str = "real", dirty: bool = False, sha: str = "abc1234"
) -> None:
    folder = runs_dir / name
    folder.mkdir(parents=True)
    config = {"variant": "rag_hybrid", "llm_mode": mode, "git_sha": sha, "git_dirty": dirty}
    check = {
        "name": "hard-handoff recall",
        "shown": "8 of 8",
        "target_text": "1.00",
        "red_line_text": "1.00",
        "status": "pass",
        "judged": False,
        "value": 1.0,
    }
    metrics = {"tickets_finished": 20, "cost_usd": "0.0194", "checks": [check]}
    (folder / "config.json").write_text(json.dumps(config))
    (folder / "metrics.json").write_text(json.dumps(metrics))


def test_evals_latest_is_the_newest_clean_real_run(
    staff: dict[str, TestClient], runs_dir: Path
) -> None:
    _eval_run(runs_dir, "2026-11-18-rag_hybrid-1111111", sha="1111111")
    _eval_run(runs_dir, "2026-11-20-rag_hybrid-2222222", sha="2222222")
    _eval_run(runs_dir, "2026-11-21-rag_hybrid-3333333-dirty", dirty=True)
    _eval_run(runs_dir, "2026-11-21-attacks-rag_hybrid-4444444")
    _eval_run(runs_dir, "2026-11-22-rag_hybrid-5555555-fake", mode="fake")
    _eval_run(runs_dir, "2026-11-22-rag_hybrid-6666666", dirty=True)

    response = staff["lead"].get("/api/v1/console/evals/latest")

    assert response.status_code == 200, response.text
    run = response.json()["run"]
    assert (run["run_id"], run["date"], run["git_sha"]) == (
        "2026-11-20-rag_hybrid-2222222",
        "2026-11-20",
        "2222222",
    )
    assert run["checks"] == [
        {
            "name": "hard-handoff recall",
            "shown": "8 of 8",
            "target_text": "1.00",
            "red_line_text": "1.00",
            "status": "pass",
            "judged": False,
        }
    ]


def test_without_a_report_evals_latest_is_null(staff: dict[str, TestClient]) -> None:
    assert staff["lead"].get("/api/v1/console/evals/latest").json() == {"run": None}


# --- settings --------------------------------------------------------------------------------


def _put(client: TestClient, **body: Any) -> Any:  # noqa: ANN401
    return client.put("/api/v1/admin/settings", json=body)


def test_a_lead_changes_settings_and_each_change_is_audited_with_its_reason(
    staff: dict[str, TestClient], urls: Urls
) -> None:
    run_sql(
        urls,
        "INSERT INTO settings (key, value) VALUES ('auto_reply_enabled', 'false'),"
        " ('confidence_threshold', '0.95'), ('daily_budget_usd', '0.5'),"
        """ ('intent_allowlist', '["payment_pending", "withdrawal_issue"]')""",
    )
    lead = staff["lead"]

    response = _put(
        lead,
        auto_reply_enabled=True,
        daily_budget_usd=0.2,
        confidence_threshold=0.95,  # unchanged: no audit record
        intent_allowlist=["withdrawal_issue"],
        reason="Demo day: tighter budget",
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "auto_reply_enabled": True,
        "confidence_threshold": 0.95,
        "daily_budget_usd": 0.2,
        "intent_allowlist": ["withdrawal_issue"],
        "allowed_intents": [
            "payment_pending",
            "withdrawal_issue",
            "card_declined",
            "kyc_verification",
            "fees_fx",
            "limits",
            "account_management",
        ],
    }
    assert lead.get("/api/v1/admin/settings").json() == response.json()
    rows = run_sql(
        urls,
        "SELECT actor_type, entity_id, details FROM audit_log"
        " WHERE action = 'settings.changed' ORDER BY id",
    )
    assert [(actor, entity) for actor, entity, _ in rows] == [
        ("operator", "auto_reply_enabled"),
        ("operator", "daily_budget_usd"),
        ("operator", "intent_allowlist"),
    ]
    assert rows[1][2] == {
        "key": "daily_budget_usd",
        "old": 0.5,
        "new": 0.2,
        "reason": "Demo day: tighter budget",
    }
    assert rows[2][2]["old"] == ["payment_pending", "withdrawal_issue"]


def test_the_allow_list_keeps_policy_order_without_duplicates(
    staff: dict[str, TestClient],
) -> None:
    response = _put(
        staff["lead"],
        intent_allowlist=["fees_fx", "payment_pending", "fees_fx"],
        reason="order check",
    )

    assert response.json()["intent_allowlist"] == ["payment_pending", "fees_fx"]


@pytest.mark.parametrize(
    "body",
    [
        {"intent_allowlist": ["dispute_chargeback"], "reason": "add a policy intent"},
        {"intent_allowlist": ["no_such_intent"], "reason": "unknown"},
        {"confidence_threshold": 0, "reason": "zero would let anything through"},
        {"confidence_threshold": 1.5, "reason": "above one"},
        {"daily_budget_usd": -1, "reason": "negative"},
        {"daily_budget_usd": 1000, "reason": "typo"},
        {"auto_reply_enabled": True},
        {"auto_reply_enabled": True, "reason": "  "},
        {"reason": "nothing to change"},
        {"auto_reply_enabled": True, "reason": "unknown key", "max_tokens": 9},
    ],
)
def test_invalid_settings_are_refused_and_nothing_is_written(
    staff: dict[str, TestClient], urls: Urls, body: dict[str, Any]
) -> None:
    response = _put(staff["lead"], **body)

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_error"
    assert run_sql(urls, "SELECT count(*) FROM settings") == [(0,)]
    assert run_sql(urls, "SELECT count(*) FROM audit_log WHERE action = 'settings.changed'") == [
        (0,)
    ]


def test_changing_settings_needs_the_csrf_token_and_the_lead_role(
    app: FastAPI, staff: dict[str, TestClient]
) -> None:
    body = {"auto_reply_enabled": True, "reason": "try"}
    assert _put(staff["operator"], **body).status_code == 403
    assert _put(staff["compliance"], **body).status_code == 403
    no_token = _api.operator_client(app, EMAILS["lead"])
    del no_token.headers["X-CSRF-Token"]
    response = _put(no_token, **body)
    assert (response.status_code, response.json()["code"]) == (403, "csrf_failed")


# --- audit -----------------------------------------------------------------------------------


def test_the_audit_page_filters_pages_and_verifies_the_chain(
    staff: dict[str, TestClient], urls: Urls
) -> None:
    lead = staff["lead"]
    for n in range(3):
        _put(lead, daily_budget_usd=0.1 + n / 10, reason=f"change {n}")

    first = lead.get(
        "/api/v1/console/audit", params={"action": "settings.changed", "limit": 2}
    ).json()
    second = lead.get(
        "/api/v1/console/audit",
        params={"action": "settings.changed", "limit": 2, "cursor": first["next_cursor"]},
    ).json()

    reasons = [item["details"]["reason"] for item in first["items"] + second["items"]]
    assert reasons == ["change 2", "change 1", "change 0"]
    assert second["next_cursor"] is None
    assert {item["entity_type"] for item in first["items"]} == {"setting"}
    verify = staff["compliance"].get("/api/v1/console/audit/verify").json()
    assert verify["ok"] is True
    assert verify["broken_id"] is None
    assert verify["checked"] >= 3
