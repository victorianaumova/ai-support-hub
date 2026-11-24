"""The pause for the operator and its end (P6-03, ai-pipeline §1, ADR-0008), against Postgres
and Qdrant with recorded model answers (fake mode).

A run on the approval or handoff route pauses in `human_review` (`runs.status = interrupted`).
The console writes the operator's decision and then resumes the run with a `ReviewDecision`;
the run ends in `respond`. A run stuck in `running` is recovered by `POST /admin/runs/recover`
or by the next read of its ticket.
"""

import asyncio
import contextlib
import dataclasses
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from hub.db.session import connect
from hub.deps import build_pipeline
from hub.domain.enums import RunStatus, TicketStatus
from hub.domain.review import ReviewDecision
from hub.graph import build as build_module
from hub.graph.build import compile_graph
from hub.graph.checkpoint import CheckpointStore
from hub.graph.context import GraphContext
from hub.graph.nodes import human_review as human_review_node
from hub.graph.nodes import respond as respond_node
from hub.graph.nodes import write as write_node
from hub.graph.recover import STUCK_AFTER_S, Recovery, recover_runs
from hub.graph.run import resume_run, thread
from hub.graph.state import TicketState
from hub.main import create_app

from ._api import (
    ACCESS_CODE,
    OPERATOR_PASSWORD,
    Urls,
    add_customer,
    add_operator,
    key,
    own_database,
    run_sql,
)
from .test_graph_run import (
    NO_ANSWER,
    NOT_RECORDED,
    SEPA,
    _container,
    _index_kb,
    _kb_services,
    _one,
    _post,
    _run,
    _settings,
    _ticket_with_message,
    _truncate,
)

DATABASE = "hub_human_review_test"
OPERATOR = "marta@tallybridge.test"
ADMIN = "admin@tallybridge.test"


@pytest.fixture(scope="module")
def review_db(postgres_url: str, qdrant_url: str) -> Iterator[Urls]:
    with own_database(postgres_url, DATABASE) as urls:
        _index_kb(urls, qdrant_url)
        yield urls


@pytest.fixture
def fresh(review_db: Urls) -> Urls:
    _truncate(review_db)
    run_sql(review_db, "TRUNCATE checkpoints, checkpoint_blobs, checkpoint_writes")
    add_customer(review_db)
    _operators(review_db)
    return review_db


def _operators(urls: Urls) -> None:
    """An operator and an admin; again after `_ticket_with_message`, which empties the tables
    (it keeps operators, but a test may call it first)."""
    run_sql(urls, "DELETE FROM operators WHERE email IN (%s, %s)", OPERATOR, ADMIN)
    add_operator(urls, OPERATOR)
    add_operator(urls, ADMIN, "admin", name="Ada Admin")


@pytest.fixture
def web(fresh: Urls, qdrant_url: str) -> Iterator[TestClient]:
    with _entered(fresh, qdrant_url) as entered:
        yield entered


@contextlib.contextmanager
def _entered(urls: Urls, qdrant_url: str) -> Iterator[TestClient]:
    """One client inside the app's lifespan, so every request, every run and every resume
    share one event loop as in the server (a client outside it runs each request in a loop of
    its own, and the checkpointer's pool belongs to one loop). It holds the customer's and the
    operator's session cookies side by side; the lifespan waits for runs and tasks at the end."""
    application = create_app(_container(urls, qdrant_url))
    with TestClient(application, base_url="https://testserver") as entered:
        yield entered


def _customer(web: TestClient) -> TestClient:
    response = web.post(
        "/api/v1/demo/session", json={"customer_ref": "CUS-0001", "access_code": ACCESS_CODE}
    )
    assert response.status_code == 200, response.text
    return web


def _operator(web: TestClient, email: str = OPERATOR) -> TestClient:
    response = web.post("/api/v1/auth/login", json={"email": email, "password": OPERATOR_PASSWORD})
    assert response.status_code == 200, response.text
    web.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return web


def _run_row(urls: Urls, ticket_id: uuid.UUID) -> tuple[Any, ...]:
    return _one(
        urls,
        "SELECT id, status, route, finished_at, error FROM runs WHERE ticket_id = %s"
        " ORDER BY started_at DESC LIMIT 1",
        ticket_id,
    )


async def _snapshot(urls: Urls, ticket_id: uuid.UUID) -> Any:  # noqa: ANN401
    store = CheckpointStore.postgres(urls[1])
    try:
        graph = await store.graph(compile_graph)
        return await graph.aget_state(thread(ticket_id))
    finally:
        await store.close()


def _detail(operator: TestClient, ticket_id: uuid.UUID) -> dict[str, Any]:
    response = operator.get(f"/api/v1/console/tickets/{ticket_id}")
    assert response.status_code == 200, response.text
    detail: dict[str, Any] = response.json()
    return detail


# --- approval ---------------------------------------------------------------------------------


def test_a_paused_run_survives_a_restart_and_an_approve_completes_it(
    fresh: Urls, qdrant_url: str
) -> None:
    """Done when of P6-03: start a run → it pauses → restart the app (a new container, its own
    pools and a newly compiled graph; the same database) → approve → the message is sent and the
    run completes."""
    with _entered(fresh, qdrant_url) as first:
        ticket_id, events = _post(_customer(first), SEPA)
    assert events[-2][1] == {"status": "pending_approval"}
    run_id, status, route, finished, _ = _run_row(fresh, ticket_id)
    assert (status, route, finished) == ("interrupted", "approval", None)

    with _entered(fresh, qdrant_url) as second:
        operator = _operator(second)
        detail = _detail(operator, ticket_id)
        response = operator.post(
            f"/api/v1/console/drafts/{detail['draft']['id']}/approve",
            json={"ticket_version": detail["version"]},
            headers=key(),
        )
        assert response.status_code == 200, response.text
        message_id = response.json()["message"]["id"]
        messages = _customer(second).get(f"/api/v1/tickets/{ticket_id}/messages").json()
    assert messages["items"][-1]["id"] == message_id
    assert messages["items"][-1]["author_label"] == "AI assistant · reviewed by Marta"

    _, status, route, finished, error = _run_row(fresh, ticket_id)
    assert (status, route, error) == ("completed", "approval", None)
    assert finished is not None
    snapshot = asyncio.run(_snapshot(fresh, ticket_id))
    assert snapshot.next == ()
    assert snapshot.values["run_id"] == str(run_id)
    assert snapshot.values["final_message_id"] == message_id
    review = snapshot.values["review"]
    assert (review.kind, review.message_id, review.body) == ("approve", message_id, None)


def test_a_customer_message_on_a_waiting_draft_makes_the_approve_stale(
    web: TestClient, fresh: Urls
) -> None:
    """From P2-07: a customer message changes the ticket's version even though the status
    stays `pending_approval`, so the operator's approve answers `409 stale_draft`; no new run
    starts (a human owns the ticket) and the paused run keeps waiting."""
    customer = _customer(web)
    ticket_id, _ = _post(customer, SEPA)
    operator = _operator(web, OPERATOR)
    detail = _detail(operator, ticket_id)

    response = customer.post(
        f"/api/v1/tickets/{ticket_id}/messages", json={"message": "Any news?"}, headers=key()
    )
    assert response.status_code == 200, response.text

    stale = operator.post(
        f"/api/v1/console/drafts/{detail['draft']['id']}/approve",
        json={"ticket_version": detail["version"]},
        headers=key(),
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "stale_draft"
    for action in ("edit", "reject"):
        body: dict[str, Any] = {"ticket_version": detail["version"]}
        body |= {"body": "Edited."} if action == "edit" else {"reason": "other"}
        refused = operator.post(
            f"/api/v1/console/drafts/{detail['draft']['id']}/{action}", json=body, headers=key()
        )
        assert refused.json()["code"] == "stale_draft", action
    [(runs,)] = run_sql(fresh, "SELECT count(*) FROM runs WHERE ticket_id = %s", ticket_id)
    assert runs == 1
    assert _run_row(fresh, ticket_id)[1] == "interrupted"
    reloaded = _detail(operator, ticket_id)
    assert reloaded["version"] > detail["version"]
    assert reloaded["status"] == "pending_approval"


def test_a_reject_without_a_reply_ends_the_run_and_later_replies_are_plain(
    web: TestClient, fresh: Urls
) -> None:
    ticket_id, _ = _post(_customer(web), SEPA)
    operator = _operator(web, OPERATOR)
    detail = _detail(operator, ticket_id)
    rejected = operator.post(
        f"/api/v1/console/drafts/{detail['draft']['id']}/reject",
        json={"ticket_version": detail["version"], "reason": "wrong_facts", "note": "Check it."},
        headers=key(),
    )
    assert rejected.status_code == 200, rejected.text
    assert _run_row(fresh, ticket_id)[1] == "completed"
    review = asyncio.run(_snapshot(fresh, ticket_id)).values["review"]
    assert (review.kind, review.reject_reason, review.message_id) == (
        "reject",
        "wrong_facts",
        None,
    )

    reply = operator.post(
        f"/api/v1/console/tickets/{ticket_id}/reply",
        json={"ticket_version": rejected.json()["ticket"]["version"], "body": "It is on its way."},
        headers=key(),
    )
    assert reply.status_code == 200, reply.text
    [(runs,)] = run_sql(fresh, "SELECT count(*) FROM runs WHERE ticket_id = %s", ticket_id)
    assert runs == 1
    assert _run_row(fresh, ticket_id)[1] == "completed"


def test_an_edit_resumes_the_run_with_the_operators_masked_text(
    web: TestClient, fresh: Urls
) -> None:
    ticket_id, _ = _post(_customer(web), SEPA)
    operator = _operator(web, OPERATOR)
    detail = _detail(operator, ticket_id)
    edited = operator.post(
        f"/api/v1/console/drafts/{detail['draft']['id']}/edit",
        json={"ticket_version": detail["version"], "body": "Card 4111 1111 1111 1111 is fine."},
        headers=key(),
    )
    assert edited.status_code == 200, edited.text
    review = asyncio.run(_snapshot(fresh, ticket_id)).values["review"]
    assert review.kind == "edit"
    assert review.body == "Card [CARD ****1111] is fine."
    assert review.message_id == edited.json()["message"]["id"]
    assert _run_row(fresh, ticket_id)[1] == "completed"


# --- handoff ----------------------------------------------------------------------------------


def test_the_first_reply_on_a_handoff_resumes_the_run_and_later_ones_are_plain(
    web: TestClient, fresh: Urls
) -> None:
    ticket_id, events = _post(_customer(web), NO_ANSWER)
    assert events[-2][1] == {"status": "handed_off"}
    assert _run_row(fresh, ticket_id)[1:3] == ("interrupted", "handoff")

    operator = _operator(web, OPERATOR)
    detail = _detail(operator, ticket_id)
    first = operator.post(
        f"/api/v1/console/tickets/{ticket_id}/reply",
        json={"ticket_version": detail["version"], "body": "Let me check that for you."},
        headers=key(),
    )
    assert first.status_code == 200, first.text
    _, status, route, finished, _ = _run_row(fresh, ticket_id)
    assert (status, route) == ("completed", "handoff")
    assert finished is not None
    values = asyncio.run(_snapshot(fresh, ticket_id)).values
    assert (values["review"].kind, values["final_message_id"]) == ("reply", first.json()["id"])

    again = operator.post(
        f"/api/v1/console/tickets/{ticket_id}/reply",
        json={"ticket_version": _detail(operator, ticket_id)["version"], "body": "Done."},
        headers=key(),
    )
    assert again.status_code == 200, again.text
    values = asyncio.run(_snapshot(fresh, ticket_id)).values
    assert values["final_message_id"] == first.json()["id"]


def test_closing_a_handoff_without_an_answer_ends_the_run_without_a_message(
    web: TestClient, fresh: Urls
) -> None:
    ticket_id, _ = _post(_customer(web), NO_ANSWER)
    operator = _operator(web, OPERATOR)
    closed = operator.post(
        f"/api/v1/console/tickets/{ticket_id}/close",
        json={"ticket_version": _detail(operator, ticket_id)["version"]},
    )
    assert closed.status_code == 200, closed.text
    assert _run_row(fresh, ticket_id)[1] == "completed"
    values = asyncio.run(_snapshot(fresh, ticket_id)).values
    assert (values["review"].kind, values["final_message_id"]) == ("close", None)


async def test_a_failure_after_the_resume_fails_the_run_and_keeps_the_answer(
    fresh: Urls, qdrant_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`respond` fails after the resume: the run is `failed` with its route, the ticket keeps
    the operator's answer (the decision was committed before the resume)."""
    ticket_id, message_id = _ticket_with_message(fresh, SEPA)
    outcome, _ = await _run(fresh, qdrant_url, ticket_id, message_id)
    assert outcome.ticket_status == TicketStatus.PENDING_APPROVAL
    run_sql(
        fresh,
        "UPDATE tickets SET status = 'answered', version = version + 1 WHERE id = %s",
        ticket_id,
    )
    real = human_review_node.record_outcome

    async def broken(session: Any, state: Any, status: RunStatus) -> None:  # noqa: ANN401
        if status == RunStatus.COMPLETED:
            raise ConnectionError
        await real(session, state, status)

    monkeypatch.setattr(respond_node, "record_outcome", broken)
    pipeline = _pipeline(fresh, qdrant_url)
    try:
        ended = await resume_run(
            pipeline,
            ticket_id=ticket_id,
            run_id=outcome.run_id,  # type: ignore[arg-type]
            decision=ReviewDecision(kind="approve", operator_id=str(uuid.uuid4())),
        )
    finally:
        await _close(pipeline)
    assert ended is False
    assert _one(
        fresh, "SELECT status, route, route_reasons, error FROM runs WHERE id = %s", outcome.run_id
    ) == ("failed", "approval", ["auto_reply_off"], "ConnectionError")
    assert _one(fresh, "SELECT status FROM tickets WHERE id = %s", ticket_id) == ("answered",)


async def test_a_resume_of_a_run_that_is_not_paused_does_nothing(
    fresh: Urls, qdrant_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("hub.graph.run.RESUME_WAIT_S", 0.2)
    ticket_id, _ = _ticket_with_message(fresh, SEPA)
    pipeline = _pipeline(fresh, qdrant_url)
    try:
        ended = await resume_run(
            pipeline,
            ticket_id=ticket_id,
            run_id=uuid.uuid4(),
            decision=ReviewDecision(kind="close", operator_id=str(uuid.uuid4())),
        )
    finally:
        await _close(pipeline)
    assert ended is False


# --- ownership rule ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("last_route", "runs", "status"),
    [("auto_reply", 2, "handed_off"), ("handoff", 1, "handed_off"), ("approval", 1, "handed_off")],
)
def test_a_follow_up_starts_a_run_only_while_the_ai_owns_the_ticket(
    web: TestClient, fresh: Urls, last_route: str, runs: int, status: str
) -> None:
    """data model §1: after the first handoff or approval a human owns the ticket. While the AI
    owns it (an auto-reply so far), a follow-up starts a new run; the unrecorded text makes that
    run fail, which hands the ticket off and proves the run started."""
    customer = _customer(web)
    ticket_id, _ = _post(customer, NOT_RECORDED)
    run_sql(
        fresh,
        "UPDATE tickets SET status = 'answered', last_route = %s WHERE id = %s",
        last_route,
        ticket_id,
    )
    response = customer.post(
        f"/api/v1/tickets/{ticket_id}/messages",
        json={"message": NOT_RECORDED + " Again."},
        headers=key(),
    )
    assert response.status_code == 200, response.text
    [(count,)] = run_sql(fresh, "SELECT count(*) FROM runs WHERE ticket_id = %s", ticket_id)
    assert count == runs
    assert _one(fresh, "SELECT status FROM tickets WHERE id = %s", ticket_id) == (status,)
    if runs == 2:
        [(trigger,)] = run_sql(
            fresh,
            "SELECT m.body FROM runs r JOIN messages m ON m.id = r.trigger_message_id"
            " WHERE r.ticket_id = %s ORDER BY r.started_at DESC LIMIT 1",
            ticket_id,
        )
        assert trigger == NOT_RECORDED + " Again."


# --- recovery ---------------------------------------------------------------------------------


def _stuck_run(urls: Urls, ticket_id: uuid.UUID, message_id: uuid.UUID) -> uuid.UUID:
    run_id = uuid.uuid4()
    run_sql(
        urls,
        "INSERT INTO runs (id, ticket_id, trigger_message_id, status, graph_version, started_at)"
        " VALUES (%s, %s, %s, 'running', 'graph-v2', now() - make_interval(secs => %s))",
        run_id,
        ticket_id,
        message_id,
        STUCK_AFTER_S + 5,
    )
    return run_id


def test_recover_restarts_a_run_that_died_before_its_first_checkpoint(
    web: TestClient, fresh: Urls
) -> None:
    ticket_id, message_id = _ticket_with_message(fresh, SEPA)
    _operators(fresh)
    run_id = _stuck_run(fresh, ticket_id, message_id)
    admin = _operator(web, ADMIN)

    response = admin.post("/api/v1/admin/runs/recover")

    assert response.status_code == 200, response.text
    assert response.json() == {"found": 1, "resumed": 1, "failed": 0}
    # The recovered run went on to its pause, under its own id.
    assert _one(fresh, "SELECT status, route FROM runs WHERE id = %s", run_id) == (
        "interrupted",
        "approval",
    )
    assert _one(fresh, "SELECT status FROM tickets WHERE id = %s", ticket_id) == (
        "pending_approval",
    )
    actor, details = _one(
        fresh, "SELECT actor_type, details FROM audit_log WHERE action = 'run.recovered'"
    )
    assert (actor, details["from"], details["ticket_id"]) == ("operator", "restart", str(ticket_id))
    assert admin.post("/api/v1/admin/runs/recover").json() == {
        "found": 0,
        "resumed": 0,
        "failed": 0,
    }


def test_recover_needs_an_admin(web: TestClient, fresh: Urls) -> None:
    _operators(fresh)
    response = _operator(web, OPERATOR).post("/api/v1/admin/runs/recover")
    assert response.status_code == 403


async def test_recover_continues_a_run_from_its_last_checkpoint(
    fresh: Urls, qdrant_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process dies in `write` (the run's task is cancelled there, as a stopped process
    would leave it): the run stays `running` with checkpoints up to `research`. Recovery
    continues from there; triage is not called again."""
    ticket_id, message_id = _ticket_with_message(fresh, SEPA)
    in_write = asyncio.Event()

    async def hangs(_state: TicketState, _ctx: GraphContext) -> TicketState:
        in_write.set()
        await asyncio.Event().wait()
        raise AssertionError  # pragma: no cover - never woken

    # The graph is compiled per checkpoint store from the node table.
    nodes = tuple(
        dataclasses.replace(spec, run=hangs) if spec.name == "write" else spec
        for spec in build_module.NODES
    )
    monkeypatch.setattr(build_module, "NODES", nodes)
    task = asyncio.create_task(_run(fresh, qdrant_url, ticket_id, message_id))
    await asyncio.wait_for(in_write.wait(), timeout=60)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    [(run_id, status)] = run_sql(
        fresh, "SELECT id, status FROM runs WHERE ticket_id = %s", ticket_id
    )
    assert status == "running"
    snapshot = await _snapshot(fresh, ticket_id)
    assert snapshot.next == ("write",)

    monkeypatch.setattr(
        build_module,
        "NODES",
        tuple(
            dataclasses.replace(spec, run=write_node.run) if spec.name == "write" else spec
            for spec in nodes
        ),
    )
    run_sql(
        fresh,
        "UPDATE runs SET started_at = now() - make_interval(secs => %s) WHERE id = %s",
        STUCK_AFTER_S + 5,
        run_id,
    )
    pipeline = _pipeline(fresh, qdrant_url)
    try:
        report = await recover_runs(pipeline, Recovery())
    finally:
        await _close(pipeline)
    assert (report.found, report.resumed, report.failed) == (1, 1, 0)
    assert _one(fresh, "SELECT status FROM runs WHERE id = %s", run_id) == ("interrupted",)
    nodes_called = [
        node
        for (node,) in run_sql(
            fresh, "SELECT node FROM llm_calls WHERE node <> 'embedding' ORDER BY created_at"
        )
    ]
    # The write/v6 recordings pass the check: no retry (P7-06).
    assert nodes_called == ["triage", "write", "verify"]
    actor, details = _one(
        fresh, "SELECT actor_type, details FROM audit_log WHERE action = 'run.recovered'"
    )
    assert (actor, details["from"]) == ("system", "checkpoint")


async def test_a_recent_or_live_run_is_not_stuck(fresh: Urls, qdrant_url: str) -> None:
    ticket_id, message_id = _ticket_with_message(fresh, SEPA)
    old = _stuck_run(fresh, ticket_id, message_id)
    run_sql(
        fresh,
        "INSERT INTO runs (id, ticket_id, trigger_message_id, status, graph_version)"
        " VALUES (%s, %s, %s, 'running', 'graph-v2')",
        uuid.uuid4(),
        ticket_id,
        message_id,
    )
    pipeline = _pipeline(fresh, qdrant_url)
    pipeline.live.claim(old)
    try:
        report = await recover_runs(pipeline, Recovery())
    finally:
        await _close(pipeline)
    assert report.found == 0


async def test_a_recovery_that_fails_hands_the_ticket_to_a_human(
    fresh: Urls, qdrant_url: str
) -> None:
    ticket_id, message_id = _ticket_with_message(fresh, SEPA)
    run_sql(fresh, "DELETE FROM messages WHERE id = %s", message_id)  # erased meanwhile
    run_id = _stuck_run(fresh, ticket_id, None)  # type: ignore[arg-type]
    pipeline = _pipeline(fresh, qdrant_url)
    try:
        report = await recover_runs(pipeline, Recovery())
    finally:
        await _close(pipeline)
    assert (report.found, report.resumed, report.failed) == (1, 0, 1)
    assert _one(fresh, "SELECT status, error FROM runs WHERE id = %s", run_id) == (
        "failed",
        "RunIncompleteError",
    )
    assert _one(fresh, "SELECT status, route_reasons FROM tickets WHERE id = %s", ticket_id) == (
        "handed_off",
        ["system_error"],
    )


def test_reading_the_ticket_recovers_its_stuck_run(fresh: Urls, qdrant_url: str) -> None:
    """design §10: the next read of the ticket starts the recovery; the read does not wait."""
    ticket_id, message_id = _ticket_with_message(fresh, SEPA)
    run_id = _stuck_run(fresh, ticket_id, message_id)
    with _entered(fresh, qdrant_url) as entered:
        response = _customer(entered).get(f"/api/v1/tickets/{ticket_id}")
        assert response.status_code == 200
    # The lifespan waited for the recovery task.
    assert _one(fresh, "SELECT status FROM runs WHERE id = %s", run_id) == ("interrupted",)
    [(recovered,)] = run_sql(fresh, "SELECT count(*) FROM audit_log WHERE action = 'run.recovered'")
    assert recovered == 1


def test_no_stuck_run_is_looked_for_once_the_ticket_left_processing(
    web: TestClient, fresh: Urls, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket_id, _ = _post(_customer(web), SEPA)
    called: list[uuid.UUID] = []

    async def spy(*args: Any, **kwargs: Any) -> list[Any]:  # noqa: ANN401
        called.append(kwargs["ticket_id"])
        return []

    monkeypatch.setattr("hub.api.resume.stuck_runs", spy)
    _detail(_operator(web, OPERATOR), ticket_id)
    _customer(web).get(f"/api/v1/tickets/{ticket_id}/messages")
    assert called == []


# --- helpers ----------------------------------------------------------------------------------


def _pipeline(urls: Urls, qdrant_url: str) -> Any:  # noqa: ANN401
    settings = _settings(urls, qdrant_url)
    database = connect(settings.database_url.get_secret_value())
    kb = _kb_services(settings, database)
    pipeline = build_pipeline(settings, database, kb.retriever)
    _OPEN[id(pipeline)] = (kb, database)
    return pipeline


_OPEN: dict[int, tuple[Any, Any]] = {}


async def _close(pipeline: Any) -> None:  # noqa: ANN401
    kb, database = _OPEN.pop(id(pipeline))
    await pipeline.checkpoints.close()
    await kb.close()
    await database.dispose()
