"""The workflow graph through the API against Postgres and Qdrant, with recorded model answers
(P3-05, retrieval P5-03, the graph and its Postgres checkpointer P6-01).

`POST /tickets` starts the graph (`intake → triage → policy_gate → research → write → verify →
decide → human_review → act → respond`); every run ends with the ticket in
`pending_approval` and a draft, or, on any error, `handed_off` with reason `system_error`. The
model answers come from `tests/fixtures/llm` (fake mode): the tickets are the ones in
`tests/fixtures/run/tickets.jsonl`, whose requests `make llm-requests` wrote with the search hits
in `tests/fixtures/run/evidence.json`. The search here runs on the real Qdrant server with the
real local models, and a test checks it finds those same hits.
"""

import asyncio
import dataclasses
import json
import uuid
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from hub.api import tickets as tickets_module
from hub.audit import ChainCheck, verify_chain
from hub.config import Settings
from hub.db.session import connect
from hub.deps import Container, build_pipeline
from hub.domain.draft import DraftAnswer
from hub.domain.enums import TicketStatus
from hub.graph import build as build_module
from hub.graph import run as run_module
from hub.graph.build import compile_graph
from hub.graph.checkpoint import CheckpointStore
from hub.graph.context import GraphContext
from hub.graph.nodes import handoff_prep
from hub.graph.nodes import human_review as human_review_node
from hub.graph.nodes import intake as intake_node
from hub.graph.nodes import research as research_node
from hub.graph.nodes import respond as respond_node
from hub.graph.nodes import triage as triage_node
from hub.graph.nodes import verify as verify_node
from hub.graph.pipeline import (
    GRAPH_VERSION,
    RUN_FAILED_DETAIL,
    SEARCH_MODES,
    SEARCH_TOP_K,
    VARIANT,
    RunOutcome,
    run_failed_problem,
)
from hub.graph.run import run_ticket
from hub.graph.state import TicketState, run_input
from hub.kb.full_text import load_external_sources, sources_from_files
from hub.kb.loader import read_kb
from hub.kb.retriever import KbChunk
from hub.kb.service import EMBEDDING_CALL, KbServices, build_kb_services, reload_kb
from hub.llm.gateway import build_gateway, embedder
from hub.llm.recorder import DbCallRecorder
from hub.llm.schemas import ProviderRequest, ProviderResponse, Usage
from hub.main import create_app

from ._api import (
    ACCESS_CODE,
    SESSION_SECRET,
    Urls,
    add_customer,
    add_operator,
    customer_client,
    key,
    operator_client,
    own_database,
    run_sql,
)
from ._compose import ROOT
from ._kb import MODEL_DIR

DATABASE = "hub_graph_run_test"
KB_DIR = ROOT / "data" / "kb"
TICKETS = {
    json.loads(line)["id"]: json.loads(line)["ticket"]
    for line in (ROOT / "apps/api/tests/fixtures/run/tickets.jsonl").read_text().splitlines()
    if line.strip()
}
SEPA, NO_ANSWER = TICKETS["run_01"], TICKETS["run_02"]
NOT_RECORDED = "This exact text has no recorded model answer, so the run must fail."
EVIDENCE = json.loads((ROOT / "apps/api/tests/fixtures/run/evidence.json").read_text())
SEPA_SECTION = "/help/withdrawal-times#sepa-withdrawals"
# P7-06: with the write/v5 recordings run_01's verify answer passes every claim, so the run makes
# no retry (the retry is covered with a verifier that never passes, P6-04).
SEPA_EVIDENCE = EVIDENCE["run_01"]


@pytest.fixture(scope="module")
def qdrant(qdrant_url: str) -> str:
    """The session's Qdrant; other modules clear it, so this module indexes it again."""
    return qdrant_url


@pytest.fixture(scope="module")
def run_db(postgres_url: str, qdrant: str) -> Iterator[Urls]:
    with own_database(postgres_url, DATABASE) as urls:
        _index_kb(urls, qdrant)
        yield urls


def _kb_services(settings: Settings, database: Any) -> KbServices:  # noqa: ANN401
    gateway = build_gateway(settings, DbCallRecorder(database), None)
    return build_kb_services(settings, embedder(gateway, EMBEDDING_CALL))


def _index_kb(urls: Urls, qdrant_url: str) -> None:
    """`make kb-load`: Postgres, then a full re-index of Qdrant (a stale collection of another
    module is replaced)."""
    import asyncio

    async def index() -> None:
        settings = _settings(urls, qdrant_url)
        database = connect(settings.database_url.get_secret_value())
        services = _kb_services(settings, database)
        try:
            await reload_kb(database, services.index, KB_DIR, full=True)
        finally:
            await services.close()
            await database.dispose()

    asyncio.run(index())


def _settings(urls: Urls, qdrant_url: str, *, auto_reply_allowed: bool = False) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        database_url=urls[1],
        qdrant_url=qdrant_url,
        embedding_cache_dir=MODEL_DIR,
        session_secret=SESSION_SECRET,
        demo_access_code=ACCESS_CODE,
        llm_mode="fake",
        models_config=ROOT / "config" / "models.yaml",
        prompts_dir=ROOT / "prompts",
        auto_reply_allowed=auto_reply_allowed,
    )


def _container(
    urls: Urls, qdrant_url: str, *, search: bool = True, auto_reply_allowed: bool = False
) -> Container:
    settings = _settings(urls, qdrant_url, auto_reply_allowed=auto_reply_allowed)
    database = connect(settings.database_url.get_secret_value())
    kb = _kb_services(settings, database)
    pipeline = build_pipeline(settings, database, kb.retriever if search else None)
    return Container(settings=settings, database=database, pipeline=pipeline, kb=kb)


def _truncate(urls: Urls) -> None:
    run_sql(
        urls,
        "TRUNCATE audit_log, llm_calls, tool_calls, drafts, runs, feedback, messages, tickets,"
        " customers, operators RESTART IDENTITY CASCADE",
    )


@pytest.fixture
def auto_reply_allowed() -> bool:
    """`AUTO_REPLY_ALLOWED` of the app; a test parametrizes it to switch the lock off."""
    return False


@pytest.fixture
def app(run_db: Urls, qdrant: str, auto_reply_allowed: bool) -> Iterator[FastAPI]:
    _truncate(run_db)
    add_customer(run_db)
    application = create_app(_container(run_db, qdrant, auto_reply_allowed=auto_reply_allowed))
    with TestClient(application):  # runs the lifespan: waits for runs at the end
        yield application


def _events(text: str) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines:
            events.append((lines["event"], json.loads(lines["data"])))
    return events


def _post(customer: TestClient, text: str) -> tuple[uuid.UUID, list[tuple[str, Any]]]:
    response = customer.post("/api/v1/tickets", json={"message": text}, headers=key())
    assert response.status_code == 200, response.text
    events = _events(response.text)
    return uuid.UUID(events[0][1]["ticket_id"]), events


def _one(urls: Urls, query: str, *params: Any) -> tuple[Any, ...]:  # noqa: ANN401
    [row] = run_sql(urls, query, *params)
    return row


def _actions(urls: Urls) -> list[str]:
    """Audit actions in order, without the demo login of the customer client."""
    rows = run_sql(urls, "SELECT action FROM audit_log ORDER BY id")
    return [action for (action,) in rows if action != "auth.demo_session"]


# --- a run that ends with a draft ------------------------------------------------------------


def test_an_operator_approves_the_sepa_draft_and_the_customer_sees_it(
    app: FastAPI, run_db: Urls
) -> None:
    """Done when of P3-05: approve sends the message with the "AI assistant · reviewed by" label
    (P3-05b), on a draft the fake-mode run wrote."""
    add_operator(run_db, "marta@tallybridge.test")
    customer = customer_client(app)
    operator = operator_client(app, "marta@tallybridge.test")
    ticket_id, _ = _post(customer, SEPA)
    detail = operator.get(f"/api/v1/console/tickets/{ticket_id}").json()
    assert detail["latest_run"]["prompt_versions"] == {
        "triage": "v1",
        "write": "v5",
        "verify": "v1",
    }
    draft = detail["draft"]
    assert draft["status"] == "pending"

    response = operator.post(
        f"/api/v1/console/drafts/{draft['id']}/approve",
        json={"ticket_version": detail["version"]},
        headers=key(),
    )

    assert response.status_code == 200, response.text
    messages = customer.get(f"/api/v1/tickets/{ticket_id}/messages").json()["items"]
    sent = messages[-1]
    assert sent["author_label"] == "AI assistant · reviewed by Marta"
    assert sent["body"] == draft["body"]
    # P5-03 done when: the customer message shows article links (title + section URL), built
    # by the server; chunk and document ids stay internal.
    assert {
        "marker": 1,
        "title": "How long does a withdrawal take?",
        "url": SEPA_SECTION,
    } in sent["citations"]
    assert all(set(citation) == {"marker", "title", "url"} for citation in sent["citations"])
    assert customer.get(f"/api/v1/tickets/{ticket_id}").json()["status"] == "answered"


def test_a_sepa_ticket_gets_a_cited_draft_and_waits_for_approval(
    app: FastAPI, run_db: Urls
) -> None:
    ticket_id, events = _post(customer_client(app), SEPA)

    assert [name for name, _ in events] == [
        "ticket.created",
        *["run.status"] * 4,
        "message",
        "ticket.status",
        "done",
    ]
    assert [data["step"] for name, data in events if name == "run.status"] == [
        "understanding",
        "searching",
        "writing",
        "checking",
    ]
    assert events[6][1] == {"status": "pending_approval"}

    status, intent, urgency, summary, route, reasons = _one(
        run_db,
        "SELECT status, intent, urgency, summary, last_route, route_reasons FROM tickets"
        " WHERE id = %s",
        ticket_id,
    )
    # write/v5 (P7-07): the check passes; auto-reply is off by default (ADR-0018), so the draft
    # waits for approval (row 7).
    assert (status, intent, route, reasons) == (
        "pending_approval",
        "withdrawal_issue",
        "approval",
        ["auto_reply_off"],
    )
    assert urgency in {"low", "normal"}
    assert summary

    run_id, run_status, run_route, versions, models, graph, cost, tokens_in, finished = _one(
        run_db,
        "SELECT id, status, route, prompt_versions, models, graph_version, total_cost_usd,"
        " input_tokens, finished_at FROM runs WHERE ticket_id = %s",
        ticket_id,
    )
    # The rule version of confidence (P6-02): groundedness, on the run and on the ticket.
    confidence, features, ticket_confidence = _one(
        run_db,
        "SELECT r.confidence, r.features, t.confidence FROM runs r JOIN tickets t"
        " ON t.id = r.ticket_id WHERE r.ticket_id = %s",
        ticket_id,
    )
    assert confidence == ticket_confidence
    assert confidence is not None
    assert 0 < confidence <= 1
    assert features["top_score"] > 0
    assert features["retry_used"] == 0.0
    assert features["n_citations"] >= 1
    # P6-03: the run waits for the operator with its outcome so far, not finished.
    assert (run_status, run_route, graph) == ("interrupted", "approval", GRAPH_VERSION)
    assert versions == {"triage": "v1", "write": "v5", "verify": "v1"}
    assert models == {
        "triage": "anthropic/claude-haiku-5-5",
        "write": "anthropic/claude-haiku-5-5",
        "verify": "anthropic/claude-haiku-5-5",
    }
    assert finished is None

    calls = run_sql(
        run_db,
        "SELECT node, prompt_version, status, cost_usd, input_tokens FROM llm_calls"
        " WHERE run_id = %s ORDER BY created_at",
        run_id,
    )
    assert [(node, version, call) for node, version, call, _, _ in calls] == [
        ("triage", "triage/v1", "ok"),
        ("write", "write/v5", "ok"),
        ("verify", "verify/v1", "ok"),
    ]
    assert cost == sum((row[3] for row in calls), Decimal(0))
    assert cost > 0
    assert tokens_in == sum(row[4] for row in calls)

    can_answer, body, citations, evidence, draft_status, missing, groundedness, unsupported = _one(
        run_db,
        "SELECT can_answer, body, citations, evidence, status, missing_information,"
        " groundedness, unsupported_claims FROM drafts WHERE run_id = %s",
        run_id,
    )
    assert (can_answer, draft_status, missing) == (True, "pending", None)
    # P5-04: the verify step stored its result on the draft.
    assert groundedness is not None
    assert 0 <= groundedness <= 1
    assert isinstance(unsupported, list)
    assert all(f"[{citation['marker']}]" in body for citation in citations)
    # P5-03 done when: every citation resolves to an existing external chunk, and its link is
    # the title of the chunk's article and the URL of the chunk's section.
    assert "withdrawal-times#0" in {citation["source_id"] for citation in citations}
    for citation in citations:
        doc_id, title, url, visibility = _one(
            run_db,
            "SELECT c.doc_id, d.title, d.url, d.visibility FROM kb_chunks c"
            " JOIN kb_documents d ON d.id = c.doc_id WHERE c.id = %s",
            citation["source_id"],
        )
        assert (citation["doc_id"], citation["title"], visibility) == (doc_id, title, "external")
        assert citation["url"].startswith(f"{url}#")
    sepa = next(c for c in citations if c["source_id"] == "withdrawal-times#0")
    assert sepa["url"] == SEPA_SECTION
    assert evidence["variant"] == VARIANT
    assert [chunk["chunk_id"] for chunk in evidence["chunks"]] == SEPA_EVIDENCE
    assert [chunk["rank"] for chunk in evidence["chunks"]] == list(range(1, len(SEPA_EVIDENCE) + 1))


async def test_the_live_search_finds_the_recorded_hits(run_db: Urls, qdrant: str) -> None:
    """The write recordings were made with `evidence.json`; the real server and models must
    return the same chunks in the same order, or fake mode would test a stale request. After a
    KB or search change: `make llm-requests` (it runs the search again) and `make record-llm`."""
    from hub.graph.nodes.intake import intake

    settings = _settings(run_db, qdrant)
    database = connect(settings.database_url.get_secret_value())
    services = _kb_services(settings, database)
    try:
        for ticket_id, text in TICKETS.items():
            hits = await services.retriever.search(
                intake(text).message_for_llm,
                "external",
                top_k=SEARCH_TOP_K,
                mode=SEARCH_MODES[VARIANT],
            )
            assert [hit.chunk_id for hit in hits] == EVIDENCE[ticket_id], ticket_id
    finally:
        await services.close()
        await database.dispose()


def test_the_run_writes_its_audit_records_without_ticket_text(app: FastAPI, run_db: Urls) -> None:
    ticket_id, _ = _post(customer_client(app), SEPA)

    assert _actions(run_db) == [
        "ticket.created",
        "intake.done",
        "triage.done",
        "draft.created",
        "verify.done",
        "route.decided",
    ]
    rows = {
        action: details
        for action, details in run_sql(run_db, "SELECT action, details FROM audit_log")
    }
    assert rows["intake.done"]["injection_score"] == 0.0
    assert rows["intake.done"]["pii_placeholders"] == {}
    triage = rows["triage.done"]
    assert (triage["intent"], triage["prompt_version"]) == ("withdrawal_issue", "triage/v1")
    assert "summary" not in triage
    assert rows["draft.created"]["prompt_version"] == "write/v5"
    assert rows["draft.created"]["variant"] == VARIANT
    assert [c["chunk_id"] for c in rows["draft.created"]["chunks"]] == SEPA_EVIDENCE
    assert rows["route.decided"]["reasons"] == ["auto_reply_off"]
    assert rows["route.decided"]["confidence"] is not None
    assert "groundedness" in rows["route.decided"]["features"]
    assert (rows["intake.done"]["language"], rows["intake.done"]["pan_found"]) == ("en", False)
    assert rows["route.decided"]["prompt_versions"] == {
        "triage": "v1",
        "write": "v5",
        "verify": "v1",
    }
    verified = rows["verify.done"]
    assert (verified["prompt_version"], verified["llm_check"]) == ("verify/v1", True)
    assert verified["policy_violations"] == []
    assert "claim" not in json.dumps(verified).replace("claims", "")
    assert rows["draft.created"]["masked"] == {}
    assert rows["triage.done"]["summary_masked"] == {}
    # The audit log holds ids, codes and numbers, never message text (security §5).
    text = json.dumps(list(rows.values()))
    assert "SEPA" not in text
    assert "withdrew" not in text

    async def check() -> ChainCheck:
        engine = create_async_engine(run_db[1])
        async with AsyncSession(engine) as session:
            result = await verify_chain(session)
        await engine.dispose()
        return result

    import asyncio

    assert asyncio.run(check()).ok
    assert ticket_id


def test_a_question_outside_the_kb_is_handed_off_with_a_notice(app: FastAPI, run_db: Urls) -> None:
    """Row 3 (P6-02): a draft that cannot answer hands the ticket off (`no_kb_answer`). The
    customer gets the notice in the stream; the draft is kept for the operator as
    `superseded`."""
    customer = customer_client(app)
    ticket_id, events = _post(customer, NO_ANSWER)

    assert [name for name, _ in events][-3:] == ["message", "ticket.status", "done"]
    assert events[-2] == ("ticket.status", {"status": "handed_off"})
    notice = events[-3][1]
    assert notice["role"] == "system"
    assert notice["body"].startswith("I could not find the answer to your question")
    assert "business hours" in notice["body"] or "minutes" in notice["body"]
    # The notice is stored: the chat shows it after a reload.
    stored = customer.get(f"/api/v1/tickets/{ticket_id}/messages").json()["items"]
    assert stored[-1]["id"] == notice["id"]
    assert stored[-1]["body"] == notice["body"]
    assert _one(
        run_db, "SELECT status, last_route, route_reasons FROM tickets WHERE id = %s", ticket_id
    ) == ("handed_off", "handoff", ["no_kb_answer"])

    can_answer, body, citations, missing, status = _one(
        run_db,
        "SELECT can_answer, body, citations, missing_information, status FROM drafts"
        " WHERE ticket_id = %s",
        ticket_id,
    )
    assert (can_answer, body, citations, status) == (False, "", [], "superseded")
    assert missing
    # P5-04: the first code check fails, so the LLM check is never called. The operator summary
    # has no recording in fake mode: the notice goes out without it (ai-pipeline §3).
    run_id, groundedness = _one(
        run_db, "SELECT run_id, groundedness FROM drafts WHERE ticket_id = %s", ticket_id
    )
    assert groundedness is None
    assert run_sql(
        run_db, "SELECT node, status FROM llm_calls WHERE run_id = %s ORDER BY created_at", run_id
    ) == [("triage", "ok"), ("write", "ok"), ("handoff_prep", "error")]
    (details,) = run_sql(
        run_db,
        "SELECT details FROM audit_log WHERE action = 'verify.done' AND details->>'run_id' = %s",
        str(run_id),
    )[0]
    assert details["policy_violations"] == ["cannot_answer"]
    assert (details["passed"], details["llm_check"]) == (False, False)
    sent = _one(run_db, "SELECT details FROM audit_log WHERE action = 'message.sent'")[0]
    assert (sent["kind"], sent["reasons"], sent["operator_summary"]) == (
        "handoff_notice",
        ["no_kb_answer"],
        False,
    )


# --- a run that fails ------------------------------------------------------------------------


def test_a_failed_model_call_hands_the_ticket_to_a_human(app: FastAPI, run_db: Urls) -> None:
    """No recording for this text: fake mode raises, the run fails, a human gets the ticket."""
    ticket_id, events = _post(customer_client(app), NOT_RECORDED)

    names = [name for name, _ in events]
    # The handoff notice is saved and streamed by `handoff_prep`, before the run ends with `error`.
    assert names == ["ticket.created", "run.status", "message", "error", "ticket.status", "done"]
    assert events[2][1]["body"].startswith("I could not prepare an answer")
    error = dict(events)["error"]
    assert (error["code"], error["status"], error["detail"]) == (
        "internal_error",
        500,
        RUN_FAILED_DETAIL,
    )
    assert events[4][1] == {"status": "handed_off"}

    assert _one(
        run_db, "SELECT status, last_route, route_reasons FROM tickets WHERE id = %s", ticket_id
    ) == ("handed_off", "handoff", ["system_error"])
    run_id, status, error_class, route = _one(
        run_db, "SELECT id, status, error, route FROM runs WHERE ticket_id = %s", ticket_id
    )
    assert (status, error_class, route) == ("failed", "FixtureMissingError", "handoff")
    assert _one(run_db, "SELECT status, error FROM llm_calls WHERE run_id = %s", run_id) == (
        "error",
        "FixtureMissing",
    )
    assert run_sql(run_db, "SELECT 1 FROM drafts WHERE ticket_id = %s", ticket_id) == []
    assert _actions(run_db)[-2:] == ["route.decided", "message.sent"]


async def test_a_run_without_a_search_is_degraded_and_the_draft_cannot_answer(
    run_db: Urls, qdrant: str
) -> None:
    """ai-pipeline §3 (P6-01): a failed research step continues with what it has. Without a
    search the writer has nothing to cite, so the draft cannot answer and the ticket is handed
    off (row 3, P6-02); the audit record says the research was degraded. The operator summary
    replaces the triage summary and never reaches the customer."""
    ticket_id, message_id = _ticket_with_message(run_db, SEPA)
    draft = {
        "can_answer": False,
        "missing_information": "The sources do not say how long a SEPA withdrawal takes.",
        "answer": "",
        "citations": [],
        "proposed_actions": [],
    }
    summary = {
        "problem": "SEPA withdrawal of 420 EUR from yesterday has not arrived.",
        "found": "Nothing yet.",
        "missing": "Check the withdrawal status and the bank details.",
    }
    provider = _Scripted([json.dumps(TRIAGE), json.dumps(draft), json.dumps(summary)])

    outcome, emitted = await _run(
        run_db, qdrant, ticket_id, message_id, provider=provider, search=False
    )

    assert (outcome.failed, outcome.ticket_status) == (False, TicketStatus.HANDED_OFF)
    assert [data["step"] for name, data in emitted if name == "run.status"] == [
        "understanding",
        "searching",
        "writing",
        "checking",
    ]
    assert provider.answers == []  # no verify call: the code check refused the empty answer
    can_answer, evidence, status = _one(
        run_db, "SELECT can_answer, evidence, status FROM drafts WHERE ticket_id = %s", ticket_id
    )
    assert (can_answer, evidence["chunks"], status) == (False, [], "superseded")
    created = _one(run_db, "SELECT details FROM audit_log WHERE action = 'draft.created'")[0]
    assert created["research_degraded"] is True

    stored_summary, reasons = _one(
        run_db, "SELECT summary, route_reasons FROM tickets WHERE id = %s", ticket_id
    )
    assert reasons == ["no_kb_answer"]
    assert stored_summary == (
        f"Problem: {summary['problem']}\nFound: {summary['found']}\nMissing: {summary['missing']}"
    )
    # The summary is for operators only: not in the notice, not anywhere in the stream.
    [(notice,)] = run_sql(run_db, "SELECT body FROM messages WHERE role = 'system'")
    assert summary["missing"] not in notice
    assert all(summary["problem"] not in json.dumps(data) for _, data in emitted)
    [(version,)] = run_sql(
        run_db,
        "SELECT prompt_version FROM llm_calls WHERE run_id = %s AND node = 'handoff_prep'",
        outcome.run_id,
    )
    assert version == "handoff_summary/v1"


LOST_CARD = "I lost my card on the train this morning. What do I do?"


def test_a_lost_card_ticket_gets_the_freeze_step_and_a_handoff_notice_in_the_stream(
    run_db: Urls, qdrant: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Done when of P6-02, through the API: triage says `card_lost_stolen`, row 1 hands the
    ticket off before research, and the stream carries the notice with the freeze step."""
    lost = {**TRIAGE, "intent": "card_lost_stolen", "urgency": "high"}
    summary = {"problem": "Card lost this morning.", "found": "Nothing yet.", "missing": "All."}
    provider = _Scripted([json.dumps(lost), json.dumps(summary)])
    _truncate(run_db)
    add_customer(run_db)
    container = _container(run_db, qdrant)
    assert container.pipeline is not None
    pipeline = dataclasses.replace(
        container.pipeline,
        gateway=dataclasses.replace(container.pipeline.gateway, provider=provider),
    )
    application = create_app(dataclasses.replace(container, pipeline=pipeline))
    with TestClient(application):
        ticket_id, events = _post(customer_client(application), LOST_CARD)

    names = [name for name, _ in events]
    assert names == [
        "ticket.created",
        "run.status",
        "message",
        "ticket.status",
        "done",
    ]  # no searching, writing or checking: the policy gate handed off before research
    notice = events[2][1]
    assert notice["role"] == "system"
    assert notice["is_ai_generated"] is False
    assert notice["body"].startswith(
        "This needs a support specialist, so I have passed your message to our team. "
        "Please freeze your card in the app now."
    )
    assert "within 30 minutes" in notice["body"] or "within 60 minutes" in notice["body"]
    assert summary["problem"] not in notice["body"]
    assert events[3][1] == {"status": "handed_off"}
    assert _one(
        run_db, "SELECT status, route_reasons, summary FROM tickets WHERE id = %s", ticket_id
    ) == (
        "handed_off",
        ["policy_intent"],
        "Problem: Card lost this morning.\nFound: Nothing yet.\nMissing: All.",
    )
    assert provider.answers == []
    assert run_sql(run_db, "SELECT 1 FROM drafts WHERE ticket_id = %s", ticket_id) == []


def _ticket_with_message(
    urls: Urls, text: str, status: str = "processing"
) -> tuple[uuid.UUID, uuid.UUID]:
    """A ticket with one customer message, written outside the app (no run started)."""
    run_sql(
        urls, "TRUNCATE audit_log, llm_calls, drafts, runs, messages, tickets, customers CASCADE"
    )
    customer_id = add_customer(urls)
    ticket_id, message_id = uuid.uuid4(), uuid.uuid4()
    run_sql(
        urls,
        "INSERT INTO tickets (id, customer_id, channel, status, ai_disclosed_at)"
        " VALUES (%s, %s, 'chat', %s, now())",
        ticket_id,
        customer_id,
        status,
    )
    run_sql(
        urls,
        "INSERT INTO messages (id, ticket_id, role, body) VALUES (%s, %s, 'customer', %s)",
        message_id,
        ticket_id,
        text,
    )
    return ticket_id, message_id


@dataclasses.dataclass
class _Scripted:
    """A provider that answers each chat request with the next scripted content."""

    answers: list[str]

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(
            content=self.answers.pop(0), usage=Usage(input_tokens=10, output_tokens=5)
        )

    async def embed(self, request: ProviderRequest) -> ProviderResponse:
        raise AssertionError("no embeddings in the run")


async def _run(
    urls: Urls,
    qdrant_url: str,
    ticket_id: uuid.UUID,
    message_id: uuid.UUID,
    on_event: Callable[[str, dict[str, Any]], None] = lambda _name, _data: None,
    provider: _Scripted | None = None,
    *,
    search: bool = True,
) -> tuple[RunOutcome, list[tuple[str, dict[str, Any]]]]:
    settings = _settings(urls, qdrant_url)
    database = connect(settings.database_url.get_secret_value())
    kb = _kb_services(settings, database)
    pipeline = build_pipeline(settings, database, kb.retriever if search else None)
    if provider is not None:
        pipeline = dataclasses.replace(
            pipeline, gateway=dataclasses.replace(pipeline.gateway, provider=provider)
        )
    emitted: list[tuple[str, dict[str, Any]]] = []

    async def emit(name: str, data: dict[str, Any]) -> None:
        emitted.append((name, data))
        on_event(name, data)

    try:
        outcome = await run_ticket(
            pipeline,
            ticket_id=ticket_id,
            message_id=message_id,
            emit=emit,
        )
    finally:
        await pipeline.checkpoints.close()
        await kb.close()
        await database.dispose()
    return outcome, emitted


async def test_a_ticket_claimed_during_the_run_keeps_its_operator(
    run_db: Urls, qdrant: str
) -> None:
    """The run still finishes; its draft is `superseded` and the ticket is left alone."""
    ticket_id, message_id = _ticket_with_message(run_db, SEPA)

    def claim(_name: str, data: dict[str, Any]) -> None:
        if data.get("step") == "writing":
            # An operator claims the ticket (the claim endpoint does processing → handed_off).
            run_sql(
                run_db,
                "UPDATE tickets SET status = 'handed_off', last_route = 'handoff',"
                " route_reasons = '[\"operator_takeover\"]', version = version + 1"
                " WHERE id = %s",
                ticket_id,
            )

    outcome, emitted = await _run(run_db, qdrant, ticket_id, message_id, claim)

    assert not outcome.failed
    assert emitted[-1] == ("ticket.status", {"status": "handed_off"})
    assert _one(run_db, "SELECT status, route_reasons FROM tickets WHERE id = %s", ticket_id) == (
        "handed_off",
        ["operator_takeover"],
    )
    assert _one(run_db, "SELECT status FROM drafts WHERE ticket_id = %s", ticket_id) == (
        "superseded",
    )
    assert "route.decided" not in _actions(run_db)


async def test_a_claim_through_the_console_during_the_run_needs_the_current_version(
    app: FastAPI, run_db: Urls, qdrant: str
) -> None:
    """P3-05a known gap: the claim goes through `POST /console/tickets/{id}/claim`. The run's
    triage save moved the version, so a claim with the version from before the run is 409; with
    the current one it hands the ticket off, and the run's draft is `superseded`."""
    ticket_id, message_id = _ticket_with_message(run_db, SEPA)
    add_operator(run_db, "marta@tallybridge.test")
    operator = operator_client(app, "marta@tallybridge.test")
    before = operator.get(f"/api/v1/console/tickets/{ticket_id}").json()["version"]
    answers: list[tuple[int, str]] = []

    def claim(_name: str, data: dict[str, Any]) -> None:
        if data.get("step") != "writing" or answers:  # the first write only
            return
        url = f"/api/v1/console/tickets/{ticket_id}/claim"
        stale = operator.post(url, json={"ticket_version": before})
        answers.append((stale.status_code, stale.json()["code"]))
        current = operator.get(f"/api/v1/console/tickets/{ticket_id}").json()["version"]
        assert current > before
        answers.append((operator.post(url, json={"ticket_version": current}).status_code, ""))

    outcome, emitted = await _run(run_db, qdrant, ticket_id, message_id, claim)

    assert answers == [(409, "stale_draft"), (200, "")]
    assert not outcome.failed
    assert emitted[-1] == ("ticket.status", {"status": "handed_off"})
    assert _one(run_db, "SELECT status, route_reasons FROM tickets WHERE id = %s", ticket_id) == (
        "handed_off",
        ["operator_takeover"],
    )
    assert _one(run_db, "SELECT status FROM drafts WHERE ticket_id = %s", ticket_id) == (
        "superseded",
    )


async def test_no_run_starts_for_a_ticket_a_human_already_has(run_db: Urls, qdrant: str) -> None:
    ticket_id, message_id = _ticket_with_message(run_db, SEPA, status="handed_off")

    outcome, emitted = await _run(run_db, qdrant, ticket_id, message_id)

    assert outcome == RunOutcome(run_id=None, ticket_status=TicketStatus.HANDED_OFF, failed=False)
    # The stream still ends with the ticket's status (api.md §4, review round 1 N3).
    assert emitted == [
        ("run.status", {"step": "understanding"}),
        ("ticket.status", {"status": "handed_off"}),
    ]
    assert run_sql(run_db, "SELECT 1 FROM runs") == []


async def test_no_run_starts_for_a_message_of_another_ticket(run_db: Urls, qdrant: str) -> None:
    ticket_id, _ = _ticket_with_message(run_db, SEPA)

    outcome, emitted = await _run(run_db, qdrant, ticket_id, uuid.uuid4())

    assert outcome.run_id is None
    assert emitted[-1] == ("ticket.status", {"status": "processing"})
    assert run_sql(run_db, "SELECT 1 FROM runs") == []


async def test_a_failure_that_cannot_be_recorded_still_ends_the_stream(
    run_db: Urls, monkeypatch: pytest.MonkeyPatch, qdrant: str
) -> None:
    """When even the handoff cannot be written (database down), the stream gets `error` and the
    ticket's real status, `processing` (it waits in the queue); the run stays `running` for
    `recover-runs` (P6-03). The model call fails (no recording), so the graph goes to
    `handoff_prep`, which cannot write either; then the run's last resort cannot."""
    ticket_id, message_id = _ticket_with_message(run_db, NOT_RECORDED)

    async def broken(*_args: object) -> None:
        raise ConnectionError

    monkeypatch.setattr(handoff_prep, "hand_off", broken)
    monkeypatch.setattr(run_module, "fail", broken)
    outcome, emitted = await _run(run_db, qdrant, ticket_id, message_id)

    assert (outcome.failed, outcome.ticket_status) == (True, None)
    assert emitted[1:] == [
        ("error", run_failed_problem()),
        ("ticket.status", {"status": "processing"}),
    ]
    assert _one(run_db, "SELECT status FROM runs WHERE ticket_id = %s", ticket_id) == ("running",)


TEST_CARD = "4111 1111 1111 1111"  # public test card number


async def test_card_data_written_by_the_model_is_stored_masked(run_db: Urls, qdrant: str) -> None:
    """Review round 1, B1 (AGENTS.md never-do #1): the model only sees masked text, but its
    answers are masked again before anything is stored."""
    ticket_id, message_id = _ticket_with_message(run_db, SEPA)
    triage = {
        "intent": "withdrawal_issue",
        "secondary_intent": None,
        "urgency": "normal",
        "wants_human": False,
        "reports_unauthorised_activity": False,
        "is_complaint_or_legal": False,
        "asks_for_action": "none",
        "injection_suspected": False,
        "summary": f"Customer with card {TEST_CARD} asks about a SEPA withdrawal.",
    }
    draft = {
        "can_answer": True,
        "missing_information": None,
        "answer": f"Your card {TEST_CARD} is not used for SEPA withdrawals [1].",
        "citations": [{"marker": 1, "source_id": "withdrawal-times#0"}],
        "proposed_actions": [
            {
                "type": "jira_create_issue",
                "issue_type": "dispute",
                "summary": f"Card {TEST_CARD}",
                "description": f"Check card {TEST_CARD}.",
            }
        ],
    }
    provider = _Scripted([json.dumps(triage), json.dumps(draft)])

    outcome, _ = await _run(run_db, qdrant, ticket_id, message_id, provider=provider)

    assert not outcome.failed
    [(summary,)] = run_sql(run_db, "SELECT summary FROM tickets WHERE id = %s", ticket_id)
    assert summary == "Customer with card [CARD ****1111] asks about a SEPA withdrawal."
    body, actions = _one(
        run_db, "SELECT body, proposed_actions FROM drafts WHERE ticket_id = %s", ticket_id
    )
    assert body == "Your card [CARD ****1111] is not used for SEPA withdrawals [1]."
    assert actions[0]["params"]["summary"] == "Card [CARD ****1111]"
    assert actions[0]["params"]["description"] == "Check card [CARD ****1111]."
    stored = run_sql(
        run_db,
        "SELECT t.summary || d.body || d.proposed_actions::text || "
        "(SELECT string_agg(details::text, ' ') FROM audit_log)"
        " FROM tickets t JOIN drafts d ON d.ticket_id = t.id WHERE t.id = %s",
        ticket_id,
    )
    assert "4111" not in str(stored)
    audit = dict(run_sql(run_db, "SELECT action, details FROM audit_log"))
    assert audit["draft.created"]["masked"] == {"PAN": 3}
    assert audit["triage.done"]["summary_masked"] == {"PAN": 1}


# --- the stream ------------------------------------------------------------------------------


def test_a_run_that_ends_without_a_status_still_ends_the_stream_by_the_contract(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """api.md §4: placeholder, `ticket.status`, `done` even when the run task ends without a
    status (review round 1, N3)."""

    async def silent(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(tickets_module, "run_ticket", silent)
    _, events = _post(customer_client(app), SEPA)

    assert [name for name, _ in events] == ["ticket.created", "message", "ticket.status", "done"]
    assert events[2][1] == {"status": "processing"}


def test_a_replayed_request_starts_no_second_run(app: FastAPI, run_db: Urls) -> None:
    customer = customer_client(app)
    headers = key()
    first = customer.post("/api/v1/tickets", json={"message": SEPA}, headers=headers)
    second = customer.post("/api/v1/tickets", json={"message": SEPA}, headers=headers)

    assert first.status_code == second.status_code == 200
    assert "run.status" not in second.text
    assert _one(run_db, "SELECT count(*) FROM runs") == (1,)


def test_the_stream_pings_while_the_run_works(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tickets_module, "PING_INTERVAL_S", 0.0001)
    response = customer_client(app).post("/api/v1/tickets", json={"message": SEPA}, headers=key())
    assert ": ping" in response.text
    assert response.text.rstrip().endswith("data: {}")


# --- the KB text and the schema --------------------------------------------------------------


async def test_the_kb_text_from_postgres_equals_the_one_from_the_files(run_db: Urls) -> None:
    """B0 (`write/v1`) reads the whole KB text from Postgres; tools that build it from the
    files (the evaluation estimate) must get the same text."""
    engine = create_async_engine(run_db[1])
    async with AsyncSession(engine) as session:
        from_db = await load_external_sources(session)
    await engine.dispose()
    assert from_db == sources_from_files(read_kb(KB_DIR))


@pytest.mark.parametrize(
    ("table", "column", "rule"),
    [
        ("llm_calls", "run_id", "SET NULL"),
        ("messages", "run_id", "SET NULL"),
        ("drafts", "run_id", "CASCADE"),
        ("runs", "trigger_message_id", "SET NULL"),
    ],
)
def test_foreign_keys_to_runs_have_the_agreed_delete_rules(
    run_db: Urls, table: str, column: str, rule: str
) -> None:
    [(delete_rule,)] = run_sql(
        run_db,
        "SELECT rc.delete_rule FROM information_schema.referential_constraints rc"
        " JOIN information_schema.key_column_usage k ON k.constraint_name = rc.constraint_name"
        " WHERE k.table_name = %s AND k.column_name = %s",
        table,
        column,
    )
    assert delete_rule == rule


def test_the_compose_api_service_sees_the_fixtures_config_and_prompts() -> None:
    """Fake mode in a compose container needs the recordings at `FIXTURES_DIR`, and the app
    loads config/ and prompts/ at start (P3-05, deferred from P3-01)."""
    from hub.llm.gateway import FIXTURES_DIR

    from ._compose import compose_config

    volumes = {
        (volume["source"], volume["target"]): volume.get("read_only", False)
        for volume in compose_config()["services"]["api"]["volumes"]
    }
    relative = FIXTURES_DIR.relative_to(ROOT)
    expected = {
        (str(ROOT / relative), str(Path("/app") / relative)),
        (str(ROOT / "config"), "/app/config"),
        (str(ROOT / "prompts"), "/app/prompts"),
    }
    assert expected <= set(volumes)
    assert all(volumes[mount] for mount in expected), "mount them read-only"


# --- the graph: checkpoints, timeouts, nodes alone (P6-01) ----------------------------------

TRIAGE = {
    "intent": "withdrawal_issue",
    "secondary_intent": None,
    "urgency": "normal",
    "wants_human": False,
    "reports_unauthorised_activity": False,
    "is_complaint_or_legal": False,
    "asks_for_action": "none",
    "injection_suspected": False,
    "summary": "Asks how long a SEPA withdrawal takes.",
}


async def test_the_run_checkpoints_its_state_in_postgres_under_the_ticket(
    run_db: Urls, qdrant: str
) -> None:
    """ai-pipeline §1: `thread_id` = ticket id, the Postgres checkpointer, written as the app
    role (the checkpoint tables got its grants from migration 0001's default privileges)."""
    ticket_id, message_id = _ticket_with_message(run_db, SEPA)
    outcome, _ = await _run(run_db, qdrant, ticket_id, message_id)
    assert outcome.ticket_status == TicketStatus.PENDING_APPROVAL

    [(count,)] = run_sql(
        run_db, "SELECT count(*) FROM checkpoints WHERE thread_id = %s", str(ticket_id)
    )
    assert count > 0
    store = CheckpointStore.postgres(run_db[1])
    try:
        graph = await store.graph(compile_graph)
        snapshot = await graph.aget_state({"configurable": {"thread_id": str(ticket_id)}})
    finally:
        await store.close()
    state = snapshot.values
    assert snapshot.next == ("human_review",)  # paused for the operator (P6-03)
    assert len(snapshot.interrupts) == 1
    assert (state["run_id"], state["route"]) == (str(outcome.run_id), "approval")
    assert state["draft"] is not None
    assert state["draft"].can_answer
    assert [chunk.chunk_id for chunk in state["evidence"].chunks] == SEPA_EVIDENCE


async def test_a_node_that_runs_too_long_hands_the_ticket_off(
    run_db: Urls, qdrant: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket_id, message_id = _ticket_with_message(run_db, SEPA)

    @dataclasses.dataclass
    class _Stuck(_Scripted):
        async def complete(self, request: ProviderRequest) -> ProviderResponse:
            await asyncio.sleep(5)
            raise AssertionError("the node timeout should have stopped this call")

    specs = [
        dataclasses.replace(spec, timeout_s=0.2) if spec.name == "triage" else spec
        for spec in build_module.NODES
    ]
    monkeypatch.setattr(build_module, "NODES", tuple(specs))
    outcome, emitted = await _run(run_db, qdrant, ticket_id, message_id, provider=_Stuck([]))

    assert (outcome.failed, outcome.ticket_status) == (True, TicketStatus.HANDED_OFF)
    assert emitted[-2:] == [
        ("error", run_failed_problem()),
        ("ticket.status", {"status": "handed_off"}),
    ]
    assert _one(
        run_db, "SELECT status, route, route_reasons, error FROM runs WHERE id = %s", outcome.run_id
    ) == ("failed", "handoff", ["system_error"], "TimeoutError")
    assert run_sql(run_db, "SELECT count(*) FROM drafts WHERE ticket_id = %s", ticket_id) == [(0,)]


async def _node_context(
    urls: Urls, qdrant_url: str, answers: list[str]
) -> tuple[GraphContext, list[tuple[str, dict[str, Any]]], Any]:
    settings = _settings(urls, qdrant_url)
    database = connect(settings.database_url.get_secret_value())
    pipeline = build_pipeline(settings, database, None)
    pipeline = dataclasses.replace(
        pipeline, gateway=dataclasses.replace(pipeline.gateway, provider=_Scripted(answers))
    )
    emitted: list[tuple[str, dict[str, Any]]] = []

    async def emit(name: str, data: dict[str, Any]) -> None:
        emitted.append((name, data))

    return GraphContext(pipeline=pipeline, emit=emit), emitted, database


def _new_run(urls: Urls, ticket_id: uuid.UUID, message_id: uuid.UUID) -> uuid.UUID:
    run_id = uuid.uuid4()
    run_sql(
        urls,
        "INSERT INTO runs (id, ticket_id, trigger_message_id, status, graph_version)"
        " VALUES (%s, %s, %s, 'running', 'graph-v2')",
        run_id,
        ticket_id,
        message_id,
    )
    return run_id


def _node_state(ticket_id: uuid.UUID, run_id: uuid.UUID, message_id: uuid.UUID) -> TicketState:
    return run_input(
        ticket_id=str(ticket_id),
        run_id=str(run_id),
        message_id=str(message_id),
        customer_ref=str(uuid.uuid4()),
    )


async def test_intake_alone_reads_the_ticket_and_replaces_contact_data(
    run_db: Urls, qdrant: str
) -> None:
    ticket_id, message_id = _ticket_with_message(run_db, "Write to ana@example.com please")
    run_id = _new_run(run_db, ticket_id, message_id)
    ctx, emitted, database = await _node_context(run_db, qdrant, [])
    try:
        update = await intake_node.run(_node_state(ticket_id, run_id, message_id), ctx)
    finally:
        await database.dispose()
    assert update["message_for_llm"] == "Write to <EMAIL_1> please"
    assert update["placeholders"] == {"<EMAIL_1>": "ana@example.com"}
    assert update["history_for_llm"] == "(no earlier messages)"
    assert emitted == []  # the wrapper sends the step, not the node
    details = _one(run_db, "SELECT details FROM audit_log WHERE action = 'intake.done'")[0]
    assert details["pii_placeholders"] == {"EMAIL": 1}
    assert "ana@" not in json.dumps(details)


async def test_triage_alone_saves_the_decision_on_the_ticket(run_db: Urls, qdrant: str) -> None:
    ticket_id, message_id = _ticket_with_message(run_db, SEPA)
    run_id = _new_run(run_db, ticket_id, message_id)
    ctx, _, database = await _node_context(run_db, qdrant, [json.dumps(TRIAGE)])
    state = _node_state(ticket_id, run_id, message_id)
    state.update(TicketState(message_for_llm=SEPA))
    try:
        update = await triage_node.run(state, ctx)
    finally:
        await database.dispose()
    assert update["triage"] is not None
    assert update["triage"].intent == "withdrawal_issue"
    assert set(update["models"]) == {"triage"}
    assert _one(run_db, "SELECT intent, urgency FROM tickets WHERE id = %s", ticket_id) == (
        "withdrawal_issue",
        "normal",
    )
    details = _one(run_db, "SELECT details FROM audit_log WHERE action = 'triage.done'")[0]
    assert details["run_id"] == str(run_id)


async def test_handoff_prep_alone_hands_the_ticket_to_a_human(run_db: Urls, qdrant: str) -> None:
    ticket_id, message_id = _ticket_with_message(run_db, SEPA)
    run_id = _new_run(run_db, ticket_id, message_id)
    ctx, _, database = await _node_context(run_db, qdrant, [])
    state = _node_state(ticket_id, run_id, message_id)
    state.update(TicketState(route="handoff", route_reasons=["budget_exceeded"]))
    try:
        update = await handoff_prep.run(state, ctx)
    finally:
        await database.dispose()
    assert update == {"ticket_status": "handed_off"}
    assert _one(
        run_db, "SELECT status, last_route, route_reasons FROM tickets WHERE id = %s", ticket_id
    ) == ("handed_off", "handoff", ["budget_exceeded"])
    details = _one(run_db, "SELECT details FROM audit_log WHERE action = 'route.decided'")[0]
    assert (details["route"], details["graph_version"]) == ("handoff", GRAPH_VERSION)


@pytest.mark.parametrize(
    ("status", "draft_status"), [("processing", "pending"), ("handed_off", "superseded")]
)
async def test_human_review_alone_stores_the_draft_for_the_operator(
    run_db: Urls, qdrant: str, status: str, draft_status: str
) -> None:
    ticket_id, message_id = _ticket_with_message(run_db, SEPA, status=status)
    run_id = _new_run(run_db, ticket_id, message_id)
    ctx, _, database = await _node_context(run_db, qdrant, [])
    chunks = [
        KbChunk(
            chunk_id="withdrawal-times#0",
            doc_id="withdrawal-times",
            title="How long does a withdrawal take?",
            section="SEPA withdrawals",
            url=SEPA_SECTION,
            text="A SEPA withdrawal takes 1 business day.",
            visibility="external",
            score=0.9,
            rank=1,
        )
    ]
    state = _node_state(ticket_id, run_id, message_id)
    state.update(
        TicketState(
            route="approval",
            route_reasons=["auto_reply_off"],
            evidence=research_node.evidence(chunks),
            draft=DraftAnswer.model_validate(
                {
                    "can_answer": True,
                    "missing_information": None,
                    "answer": "It takes 1 business day [1].",
                    "citations": [{"marker": 1, "source_id": chunks[0].chunk_id}],
                    "proposed_actions": [],
                }
            ),
            verification=verify_node.unavailable(),
            verify_error="StructuredOutputError",
            models={"write": "w"},
        )
    )
    try:
        update = await human_review_node.prepare(state, ctx)
        # Safe to run again (ADR-0008: a recovered run may repeat it): one draft, one route.
        again = await human_review_node.prepare(state, ctx)
    finally:
        await database.dispose()
    expected = TicketStatus.PENDING_APPROVAL if status == "processing" else TicketStatus(status)
    assert update == again == (expected, True)
    assert _one(run_db, "SELECT status FROM drafts WHERE ticket_id = %s", ticket_id) == (
        draft_status,
    )
    [(routes,)] = run_sql(run_db, "SELECT count(*) FROM audit_log WHERE action = 'route.decided'")
    assert routes == (1 if status == "processing" else 0)
    # P6-03: the run waits for the operator, with its outcome so far.
    assert _one(run_db, "SELECT status, route, finished_at FROM runs WHERE id = %s", run_id) == (
        "interrupted",
        "approval",
        None,
    )
    verified = _one(run_db, "SELECT details FROM audit_log WHERE action = 'verify.done'")[0]
    assert (verified["llm_check"], verified["error"]) == (False, "StructuredOutputError")


@pytest.mark.parametrize(
    ("errors", "expected"),
    [([], ("completed", None)), (["write:LlmUnavailableError"], ("failed", "LlmUnavailableError"))],
)
async def test_respond_alone_records_the_runs_outcome(
    run_db: Urls, qdrant: str, errors: list[str], expected: tuple[str, str | None]
) -> None:
    ticket_id, message_id = _ticket_with_message(run_db, SEPA)
    run_id = _new_run(run_db, ticket_id, message_id)
    ctx, _, database = await _node_context(run_db, qdrant, [])
    state = _node_state(ticket_id, run_id, message_id)
    state.update(
        TicketState(route="handoff" if errors else "approval", errors=errors, models={"write": "w"})
    )
    try:
        assert await respond_node.run(state, ctx) == {}
    finally:
        await database.dispose()
    status, error, models, finished = _one(
        run_db, "SELECT status, error, models, finished_at FROM runs WHERE id = %s", run_id
    )
    assert (status, error) == expected
    assert models == {"write": "w"}
    assert finished is not None


# --- the kill switch (P10-09) ----------------------------------------------------------------


def _settings_put(lead: TestClient, **changes: Any) -> None:  # noqa: ANN401
    response = lead.put("/api/v1/admin/settings", json={**changes, "reason": "test of the switch"})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("auto_reply_allowed", [True])
def test_switching_auto_reply_off_sends_the_next_ticket_to_approval(
    app: FastAPI, run_db: Urls
) -> None:
    """Done when of P10-09: a lead's `PUT /admin/settings` counts for the very next run."""
    add_customer(run_db, "CUS-0002", "Peter Horvat")
    add_operator(run_db, "lena@tallybridge.test", "lead", name="Lena Lead")
    lead = operator_client(app, "lena@tallybridge.test")
    try:
        _settings_put(
            lead,
            auto_reply_enabled=True,
            confidence_threshold=0.01,
            intent_allowlist=["withdrawal_issue"],
        )
        on_id, on_events = _post(customer_client(app), SEPA)
        on_route = _one(run_db, "SELECT route, route_reasons FROM runs WHERE ticket_id = %s", on_id)
        assert on_route == ("auto_reply", [])
        assert ("ticket.status", {"status": "answered"}) in on_events

        _settings_put(lead, auto_reply_enabled=False)
        off_id, off_events = _post(customer_client(app, "CUS-0002"), SEPA)

        off_route = _one(
            run_db, "SELECT route, route_reasons FROM runs WHERE ticket_id = %s", off_id
        )
        assert off_route == ("approval", ["auto_reply_off"])
        assert ("ticket.status", {"status": "pending_approval"}) in off_events
        changed = run_sql(
            run_db,
            "SELECT details->>'key', details->>'reason' FROM audit_log"
            " WHERE action = 'settings.changed' ORDER BY id",
        )
        assert changed == [
            ("auto_reply_enabled", "test of the switch"),
            ("confidence_threshold", "test of the switch"),
            ("intent_allowlist", "test of the switch"),
            ("auto_reply_enabled", "test of the switch"),
        ]
    finally:
        # Settings are not truncated between tests of this module; the others expect none.
        run_sql(run_db, "DELETE FROM settings")


def test_a_stored_auto_reply_setting_is_ignored_while_the_deployment_does_not_allow_it(
    app: FastAPI, run_db: Urls
) -> None:
    """ADR-0019 (owner decision, P10-02), defence in depth: a row switched on behind the API's
    back still sends the draft to an operator."""
    run_sql(
        run_db,
        "INSERT INTO settings (key, value) VALUES ('auto_reply_enabled', 'true'),"
        " ('confidence_threshold', '0.01'), ('intent_allowlist', '[\"withdrawal_issue\"]')",
    )
    try:
        ticket_id, events = _post(customer_client(app), SEPA)
        route = _one(
            run_db, "SELECT route, route_reasons FROM runs WHERE ticket_id = %s", ticket_id
        )
        assert route == ("approval", ["auto_reply_off"])
        assert ("ticket.status", {"status": "pending_approval"}) in events
    finally:
        run_sql(run_db, "DELETE FROM settings")
