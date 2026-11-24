"""Pure parts of the evaluation runner (P4-03): ticket loading, resume bookkeeping, the cost
estimate and limit, the summary files. The whole run against Postgres is tested in
`apps/api/tests/integration/test_eval_runner.py`."""

import json
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from hub.config import Settings
from hub.db.session import connect
from hub.domain.enums import LlmCallMode, TicketStatus
from hub.domain.ticket_status import can_transition
from hub.graph.draft import b0_evidence
from hub.kb.chunk_text import render_chunks
from hub.kb.full_text import KbSource, render_kb
from hub.kb.retriever import KbChunk
from hub.llm.gateway import CachingProvider
from hub.llm.schemas import BudgetExceededError, LlmUnavailableError, ProviderResponse
from hub_evals import judges as j
from hub_evals import report as reporting
from hub_evals import results as files
from hub_evals.runner import (
    ESTIMATED_CITATIONS,
    RESEARCH_TURNS,
    TICKETS_FILE,
    CostLimit,
    EstimateEvidence,
    EvalSetupError,
    Report,
    build_pipeline,
    cited_sources,
    close_path,
    describe,
    estimate_cost,
    estimate_tokens,
    eval_database_url,
    files_sha256,
    git_state,
    history_turns,
    judge_evidence,
    judge_requests,
    judge_ticket,
    load_tickets,
    main,
    masked_counts,
    parse_args,
    pending,
    run_name,
    same_database,
)
from hub_evals.schemas import HistoryMessage, Ticket

from ._gateway import MemoryRecorder, answer, make_gateway, no_calls, queue
from ._judge_requests import judges as load_test_judges

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_CONFIG = {
    "models_config": ROOT / "config" / "models.yaml",
    "prompts_dir": ROOT / "prompts",
}
JUDGES = load_test_judges()
# A public test card number (data_check list).
TEST_CARD = "4242 4242 4242 4242"


def _tickets() -> list[Ticket]:
    return load_tickets(TICKETS_FILE)


def _result(ticket_id: str, *, paid: str = "0", route: str = "approval") -> files.TicketResult:
    return files.TicketResult(
        ticket_id=ticket_id,
        run_id=None,
        ticket_status="pending_approval",
        run_status="completed",
        run_failed=False,
        route=route,
        route_reasons=["auto_reply_off"],
        error=None,
        triage=files.Triage(intent="limits", secondary_intent=None, urgency="low", summary="s"),
        draft=None,
        models={"triage": "m"},
        prompt_versions={"triage": "v1"},
        cost_usd=Decimal("0.004"),
        paid_cost_usd=Decimal(paid),
        input_tokens=10,
        output_tokens=5,
        llm_calls=2,
        latency_ms=12,
    )


def _config(**changes: object) -> files.RunConfig:
    values: dict[str, object] = {
        "variant": "b0_full_kb",
        "llm_mode": "fake",
        "graph_version": "linear-v1",
        "models": {"triage": ["a", "b"]},
        "prompt_versions": {"triage": "v1"},
        "git_sha": "abc1234",
        "git_dirty": False,
        "core_anchor_date": None,
        "tickets_file": "data/eval/tickets.dev.jsonl",
        "tickets_sha256": "0" * 64,
    }
    return files.RunConfig.model_validate(values | changes)


# --- tickets ------------------------------------------------------------------------------------


def test_load_tickets_reads_the_dev_set_and_applies_the_limit() -> None:
    assert len(_tickets()) == 20
    assert [ticket.id for ticket in load_tickets(TICKETS_FILE, 3)] == [
        ticket.id for ticket in _tickets()[:3]
    ]


def test_the_test_set_is_never_read(tmp_path: Path) -> None:
    forbidden = tmp_path / "tickets.test.jsonl"
    forbidden.write_text("", encoding="utf-8")
    with pytest.raises(EvalSetupError, match="rule 7"):
        load_tickets(forbidden)


def test_a_missing_or_invalid_ticket_file_is_a_setup_error(tmp_path: Path) -> None:
    with pytest.raises(EvalSetupError, match="not found"):
        load_tickets(tmp_path / "none.jsonl")
    broken = tmp_path / "broken.jsonl"
    broken.write_text('\n{"id": "T-1"}\n', encoding="utf-8")
    with pytest.raises(EvalSetupError, match="line 2 is invalid"):
        load_tickets(broken)


def test_close_path_keeps_to_the_state_diagram() -> None:
    """P12: every open status reaches `closed` through allowed steps only."""
    for status in TicketStatus:
        path = close_path(status)
        assert (path[-1] if path else status) == TicketStatus.CLOSED
        steps = [status, *path]
        assert all(can_transition(a, b) for a, b in pairwise(steps))
    assert close_path(TicketStatus.CLOSED) == []
    assert close_path(TicketStatus.PENDING_APPROVAL) == [
        TicketStatus.HANDED_OFF,
        TicketStatus.CLOSED,
    ]


def test_pending_skips_finished_tickets() -> None:
    tickets = _tickets()[:3]
    assert [t.id for t in pending(tickets, [_result(tickets[1].id)])] == [
        tickets[0].id,
        tickets[2].id,
    ]


def test_history_and_message_are_masked_like_the_api_masks_them() -> None:
    ticket = _tickets()[0].model_copy(
        update={"history": [HistoryMessage(role="customer", text=f"my card {TEST_CARD}")]}
    )
    (turn,) = history_turns(ticket)
    assert TEST_CARD not in turn.text
    assert turn.role == "customer"


def test_masked_counts() -> None:
    from hub.guardrails.masking import MaskResult, mask

    assert masked_counts(mask(f"card {TEST_CARD}")) == {"PAN": 1}
    assert masked_counts(MaskResult(text="x", counts={}, failed=True)) == {"failed": 1}


# --- run directory and resume -------------------------------------------------------------------


def test_run_name_marks_uncommitted_changes() -> None:
    assert run_name("2026-11-17", "b0_full_kb", "abc1234", dirty=False) == (
        "2026-11-17-b0_full_kb-abc1234"
    )
    assert run_name("2026-11-17", "b0_full_kb", "abc1234", dirty=True).endswith("-dirty")
    assert run_name("2026-11-17", "b0_full_kb", "abc1234", dirty=False, llm_mode="fake").endswith(
        "abc1234-fake"
    )


def test_git_state_without_git(tmp_path: Path) -> None:
    assert git_state(tmp_path) == ("nogit", None)


def test_git_state_hashes_the_uncommitted_changes() -> None:
    sha, diff = git_state()
    assert sha != "nogit"
    assert diff is None or len(diff) == 64


def test_files_sha256_sees_content_and_names(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("one", encoding="utf-8")
    first = files_sha256([tmp_path / "a.md"], tmp_path)
    (tmp_path / "a.md").write_text("two", encoding="utf-8")
    assert files_sha256([tmp_path / "a.md"], tmp_path) != first
    (tmp_path / "a.md").rename(tmp_path / "b.md")
    assert files_sha256([tmp_path / "b.md"], tmp_path) != first


def test_same_database_ignores_login_and_query() -> None:
    assert same_database(
        "postgresql+psycopg://hub_api:x@db:5432/hub", "postgresql+psycopg://other@db/hub?sslmode=x"
    )
    assert not same_database("postgresql+psycopg://a@db/hub", "postgresql+psycopg://a@db/hub_eval")
    assert not same_database("postgresql+psycopg://a@db/hub", "postgresql+psycopg://a@other/hub")


def test_a_cut_off_line_is_dropped_and_the_first_result_wins(tmp_path: Path) -> None:
    path = tmp_path / files.RESULTS_FILE
    files.append_result(path, _result("T-0001"))
    files.append_result(path, _result("T-0002"))
    files.append_result(path, _result("T-0001", route="handoff"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_result("T-0003").model_dump_json()[:40])  # an interrupted write
    done = files.read_results(path)
    assert [(r.ticket_id, r.route) for r in done] == [
        ("T-0001", "approval"),
        ("T-0002", "approval"),
    ]
    files.rewrite_results(path, done)
    assert list(tmp_path.glob("*.tmp")) == []
    files.append_result(path, _result("T-0003"))
    assert [r.ticket_id for r in files.read_results(path)] == ["T-0001", "T-0002", "T-0003"]


def test_a_missing_results_file_is_empty(tmp_path: Path) -> None:
    assert files.read_results(tmp_path / "none.jsonl") == []
    assert files.read_config(tmp_path / "none.json") is None


def test_config_round_trip_and_differences(tmp_path: Path) -> None:
    path = tmp_path / files.CONFIG_FILE
    files.write_config(path, _config())
    assert files.read_config(path) == _config()
    # How the run is done may change on resume; what it answers may not.
    assert files.config_differences(_config(), _config(limit=3, cache=False, concurrency=1)) == []
    assert files.config_differences(
        _config(), _config(prompt_versions={"triage": "v2"}, llm_mode="real")
    ) == ["llm_mode", "prompt_versions"]


# --- cost ---------------------------------------------------------------------------------------


async def test_the_cost_limit_refuses_only_when_enforced_and_reached() -> None:
    limit = CostLimit(limit=Decimal("0.10"), enforced=True)
    await limit.check()
    limit.add(Decimal("0.10"))
    assert limit.reached
    with pytest.raises(BudgetExceededError):
        await limit.check()
    fake = CostLimit(limit=Decimal(0), enforced=False)
    await fake.check()
    assert not fake.reached


def _pipeline(
    tmp_path: Path, *, cache: bool, variant: str = "b0_full_kb"
) -> tuple[object, CachingProvider | None]:
    settings = Settings(_env_file=None, llm_mode="real", **FIXTURE_CONFIG)  # type: ignore[arg-type]
    database = connect("postgresql+psycopg://nobody@localhost:1/none")  # never connected
    pipeline, _, provider = build_pipeline(
        settings, database, variant=variant, cache_dir=tmp_path if cache else None, tools=False
    )
    return pipeline, provider


def _evidence(tickets: list[Ticket], kb: str = "kb text") -> dict[str, EstimateEvidence]:
    return {ticket.id: EstimateEvidence(kb, (kb,)) for ticket in tickets}


def test_build_pipeline_uses_eval_context_and_the_cache(tmp_path: Path) -> None:
    from hub.domain.enums import LlmCallContext

    pipeline, cache = _pipeline(tmp_path, cache=True)
    assert pipeline.context == LlmCallContext.EVAL  # type: ignore[attr-defined]
    assert isinstance(cache, CachingProvider)
    assert _pipeline(tmp_path, cache=False)[1] is None


def test_the_estimate_prices_two_calls_per_ticket_and_cached_calls_are_free(
    tmp_path: Path,
) -> None:
    from hub.graph.pipeline import Pipeline

    pipeline, cache = _pipeline(tmp_path, cache=True)
    assert isinstance(pipeline, Pipeline)
    assert cache is not None
    tickets = _tickets()[:2]
    first = estimate_cost(pipeline, tickets, _evidence(tickets), cache)
    assert (first.calls, first.cached_calls) == (4, 0)
    assert first.cost_usd > 0
    # Store an answer for the first ticket's triage request: that call is free now.
    from hub.domain.triage import TriageResult
    from hub.graph.nodes.intake import intake
    from hub.graph.nodes.triage import triage_messages
    from hub.guardrails.masking import mask
    from hub.llm.gateway import write_fixture

    request = pipeline.gateway.chat_requests(
        "triage",
        triage_messages(
            pipeline.triage_prompt,
            intake(mask(tickets[0].message).text, history_turns(tickets[0])).message_for_llm,
        ),
        TriageResult,
    )[0]
    (tmp_path / "real").mkdir()
    write_fixture(tmp_path / "real", request, ProviderResponse(content="{}"))
    second = estimate_cost(pipeline, tickets, _evidence(tickets), cache)
    assert (second.calls, second.cached_calls) == (4, 1)
    assert second.cost_usd < first.cost_usd
    assert estimate_tokens(request) > 0


# --- settings and command line ------------------------------------------------------------------


def test_the_demo_database_is_refused() -> None:
    with pytest.raises(EvalSetupError, match="empty"):
        eval_database_url(Settings(_env_file=None))
    same = "postgresql+psycopg://a@h/hub"
    with pytest.raises(EvalSetupError, match="demo database"):
        eval_database_url(Settings(_env_file=None, database_url=same, eval_database_url=same))
    with pytest.raises(EvalSetupError, match="demo database"):
        eval_database_url(
            Settings(
                _env_file=None,
                database_url=same,
                eval_database_url="postgresql+psycopg://b:pw@h:5432/hub?application_name=x",
            )
        )


def test_parse_args() -> None:
    options = parse_args(["--limit", "10", "--no-cache", "--restart", "--concurrency", "2"])
    assert (options.limit, options.cache, options.restart, options.concurrency) == (
        10,
        False,
        True,
        2,
    )
    assert parse_args([]).cache
    with pytest.raises(SystemExit):
        parse_args(["--limit", "0"])
    assert parse_args([]).variant == "rag_hybrid"
    assert parse_args(["--variant", "rag_dense"]).variant == "rag_dense"
    assert parse_args(["--variant", "b0_full_kb"]).variant == "b0_full_kb"
    with pytest.raises(SystemExit):
        parse_args(["--variant", "rag_rerank"])


def test_main_reports_a_setup_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("EVAL_DATABASE_URL", "")
    monkeypatch.chdir(Path(__file__).parent)  # no .env here
    assert main(["--limit", "1"]) == 1
    assert "EVAL_DATABASE_URL is empty" in capsys.readouterr().err


def test_describe_names_counts_only(tmp_path: Path) -> None:
    report = Report(
        run_dir=tmp_path / "r",
        planned=10,
        skipped=4,
        ran=6,
        stopped_at_cost_limit=True,
        stopped_by_estimate=True,
        unavailable=2,
        tickets_closed=3,
    )
    lines = describe(report, Settings(_env_file=None))
    assert any("2 tickets failed because the model was unavailable" in line for line in lines)
    assert any("estimate is above" in line for line in lines)
    assert any("10 tickets, 4 already finished, 6 run now" in line for line in lines)
    assert any("EVAL_MAX_COST_USD" in line for line in lines)
    assert "eval: closed 3 open tickets of earlier starts" in lines


# --- summary ------------------------------------------------------------------------------------


def test_summary_and_report(tmp_path: Path) -> None:
    done = [_result("T-0001", paid="0.003"), _result("T-0002", route="handoff")]
    data = reporting.evaluate(_tickets(), done, planned=3)
    assert data["tickets_finished"] == 2
    assert data["routes"] == {"approval": 1, "handoff": 1}
    assert data["paid_cost_usd"] == "0.003"
    assert data["cost_per_ticket_usd"] == "0.004"
    files.write_metrics(tmp_path / files.METRICS_FILE, data)
    assert json.loads((tmp_path / files.METRICS_FILE).read_text())["tickets_planned"] == 3
    report = reporting.render_report(_config(), data, run_name="r1")
    assert "2 of 3" in report
    assert "Fake models" in report
    assert "demo check" in report.lower()
    assert "unvalidated, same model family" in report
    assert reporting.summary([], planned=0)["cost_per_ticket_usd"] is None
    real = reporting.render_report(_config(llm_mode="real"), data, run_name="r")
    assert "Fake models" not in real


# --- judges (P4-05) -----------------------------------------------------------------------------


def _chunk(chunk_id: str, text: str, rank: int) -> KbChunk:
    doc_id = chunk_id.split("#")[0]
    return KbChunk(
        chunk_id=chunk_id,
        doc_id=doc_id,
        title=doc_id,
        section="s",
        url=f"/help/{doc_id}#s",
        text=text,
        visibility="external",
        score=1.0,
        rank=rank,
    )


def _rag_draft(body: str) -> Any:  # noqa: ANN401
    chunks = [
        _chunk("withdrawal-times#0", "A SEPA withdrawal costs €1.50.", 1),
        _chunk("fees-overview#2", "Receiving money is free.", 2),
    ]
    return files.DraftOut(
        can_answer=True,
        body=body,
        missing_information=None,
        citations=[
            {"marker": 1, "source_id": "withdrawal-times#0"},
            {"marker": 2, "source_id": "gone#9"},
        ],
        evidence={
            "variant": "rag_hybrid",
            "chunks": [c.model_dump(mode="json") for c in chunks],
            "dropped_markers": [],
        },
        proposed_actions=[],
    )


def test_a_rag_draft_is_judged_against_every_chunk_the_writer_read() -> None:
    draft = _rag_draft("x")
    evidence = judge_evidence(draft, {})
    read = [
        _chunk("withdrawal-times#0", "A SEPA withdrawal costs €1.50.", 1),
        _chunk("fees-overview#2", "Receiving money is free.", 2),
    ]
    assert evidence.text == render_chunks(read)
    # Only the cited chunk the writer read; an unknown id is left to the code check.
    assert evidence.cited == {1: ("withdrawal-times#0", render_chunks(read[:1]))}
    assert draft.ranked_chunk_ids == ["withdrawal-times#0", "fees-overview#2"]


def test_a_b0_draft_is_judged_against_the_cited_documents() -> None:
    by_id = {source.id: source for source in _sources()}
    evidence = judge_evidence(_draft("x"), by_id)
    assert evidence.text == render_kb([by_id["withdrawal-times"]])
    assert evidence.cited == {1: ("withdrawal-times", render_kb([by_id["withdrawal-times"]]))}
    assert _draft("x").ranked_chunk_ids is None


async def test_a_rag_citation_is_judged_against_its_chunk() -> None:
    citation = j.CitationVerdict(quote="A SEPA withdrawal costs €1.50", supports=True)
    claims = j.FaithfulnessVerdict(
        claims=[j.ClaimCheck(claim="fee", evidence_quote="costs €1.50", supported=True)]
    )
    ticket = _tickets()[0].model_copy(update={"key_facts": []})
    provider = queue(answer(claims), answer(citation))
    body = "A SEPA withdrawal costs €1.50. [1] Receiving is free. [2]"
    result = await judge_ticket(make_gateway(provider), JUDGES, ticket, _rag_draft(body), {})
    assert [(c.marker, c.source_id, c.supports) for c in result.citations] == [
        (1, "withdrawal-times#0", True)
    ]
    faithfulness, cited = (str(r.messages[-1]["content"]) for r in provider.requests)
    assert "Source id: withdrawal-times#0" in faithfulness
    assert "Source id: fees-overview#2" in faithfulness
    assert "Source id: withdrawal-times#0" in cited
    assert "fees-overview" not in cited


def _sources() -> list[KbSource]:
    return [
        KbSource("withdrawal-times", "Withdrawal times", "u1", ("A SEPA withdrawal costs €1.50.",)),
        KbSource("fees-overview", "Fees", "u2", ("Receiving money is free.",)),
    ]


def _draft(body: str, *, can_answer: bool = True, citations: list[Any] | None = None) -> Any:  # noqa: ANN401
    return files.DraftOut(
        can_answer=can_answer,
        body=body,
        missing_information=None,
        citations=citations
        if citations is not None
        else [{"marker": 1, "source_id": "withdrawal-times"}, {"marker": 2, "source_id": "x"}],
        evidence={"variant": "b0_full_kb", "documents": [], "dropped_markers": []},
        proposed_actions=[],
    )


def test_cited_sources_maps_markers_to_known_documents() -> None:
    by_id = {source.id: source for source in _sources()}
    cited = cited_sources(_draft("x"), by_id)
    assert {marker: source.id for marker, source in cited.items()} == {1: "withdrawal-times"}


async def test_judge_ticket_calls_each_judge_and_tallies_the_paid_calls() -> None:
    by_id = {source.id: source for source in _sources()}
    ticket = _tickets()[0]
    facts = j.KeyFactsVerdict(
        facts=[
            j.FactCheck(fact=n, quote="", present=False)
            for n in range(1, len(ticket.key_facts) + 1)
        ]
    )
    claims = j.FaithfulnessVerdict(
        claims=[j.ClaimCheck(claim="fee", evidence_quote="costs €1.50", supported=True)]
    )
    citation = j.CitationVerdict(quote="A SEPA withdrawal costs €1.50", supports=True)
    provider = queue(
        answer(facts),
        answer(claims, replayed=True),  # from the cache: nothing paid
        answer(citation),
    )
    recorder = MemoryRecorder()
    gateway = make_gateway(provider, recorder, LlmCallMode.REAL)
    body = "A SEPA withdrawal costs €1.50. [1] Something without a citation. [2]"
    result = await judge_ticket(gateway, JUDGES, ticket, _draft(body), by_id)

    assert result.key_facts is not None
    assert result.key_facts.coverage == 0.0
    assert result.faithfulness is not None
    assert result.faithfulness.faithful
    # Marker [2] names an unknown document: left to the code check, no judge call.
    assert [(c.marker, c.source_id, c.supports) for c in result.citations] == [
        (1, "withdrawal-times", True)
    ]
    assert result.errors == {}
    assert result.llm_calls == 3 == len(recorder.rows)
    assert result.cost_usd == sum((row.cost_usd for row in recorder.rows), Decimal(0))
    assert Decimal(0) < result.paid_cost_usd < result.cost_usd
    # The evidence of faithfulness is the cited document only.
    evidence = provider.requests[1].messages[-1]["content"]
    assert "Document id: withdrawal-times" in evidence
    assert "fees-overview" not in evidence


async def test_judge_ticket_without_an_answer_judges_only_the_key_facts() -> None:
    provider = no_calls()  # an empty draft states no key fact: no call at all
    result = await judge_ticket(
        make_gateway(provider), JUDGES, _tickets()[0], _draft("", can_answer=False), {}
    )
    assert result.key_facts is not None
    assert result.key_facts.coverage == 0.0
    assert result.faithfulness is None
    assert result.citations == []
    assert result.llm_calls == 0


async def test_a_failing_judge_is_noted_and_an_outage_is_raised() -> None:
    from hub.llm.schemas import ProviderError

    ticket = _tickets()[0].model_copy(update={"key_facts": []})
    broken = queue(answer({"x": 1}), answer({"x": 2}), answer({"x": 3}), answer({"x": 4}))
    result = await judge_ticket(
        make_gateway(broken), JUDGES, ticket, _draft("Fee. [1]"), {s.id: s for s in _sources()}
    )
    assert result.errors == {
        "faithfulness": "StructuredOutputError",
        "citation": "StructuredOutputError",
    }
    assert result.key_facts is None

    outage = ProviderError("APIConnectionError", transient=False)
    with pytest.raises(LlmUnavailableError):
        await judge_ticket(
            make_gateway(queue(outage, outage)), JUDGES, ticket, _draft("Fee. [1]"), {}
        )


def _no_facts_present(ticket: Ticket) -> j.KeyFactsVerdict:
    return j.KeyFactsVerdict(
        facts=[
            j.FactCheck(fact=n, quote="", present=False)
            for n in range(1, len(ticket.key_facts) + 1)
        ]
    )


async def test_paid_judge_calls_count_against_the_limit_also_on_an_outage() -> None:
    from hub.llm.schemas import ProviderError

    ticket = _tickets()[0]
    outage = ProviderError("APIConnectionError", transient=False)
    recorder = MemoryRecorder()
    provider = queue(answer(_no_facts_present(ticket)), outage, outage)
    limit = CostLimit(limit=Decimal(10), enforced=True)
    with pytest.raises(LlmUnavailableError):
        await judge_ticket(
            make_gateway(provider, recorder, LlmCallMode.REAL),
            JUDGES,
            ticket,
            _draft("A SEPA withdrawal costs \u20ac1.50. [1]"),
            {s.id: s for s in _sources()},
            limit,
        )
    # The key-facts call was paid before faithfulness failed: the limit has it.
    paid = sum((row.cost_usd for row in recorder.rows if row.mode == LlmCallMode.REAL), Decimal(0))
    assert paid > 0
    assert limit.spent == paid


async def test_every_judge_call_checks_the_limit() -> None:
    ticket = _tickets()[0]
    provider = queue(answer(_no_facts_present(ticket)))
    # Below the price of one judge call: the first call is allowed, the second refused.
    limit = CostLimit(limit=Decimal("0.000001"), enforced=True)
    with pytest.raises(BudgetExceededError):
        await judge_ticket(
            make_gateway(provider, MemoryRecorder(), LlmCallMode.REAL),
            JUDGES,
            ticket,
            _draft("A SEPA withdrawal costs \u20ac1.50. [1]"),
            {s.id: s for s in _sources()},
            limit,
        )
    assert len(provider.requests) == 1
    assert limit.spent > limit.limit
    # Fake mode: the limit is not enforced and every judge runs.
    fake = CostLimit(limit=Decimal(0), enforced=False)
    result = await judge_ticket(
        make_gateway(queue(answer(_no_facts_present(ticket)))),
        JUDGES,
        ticket,
        _draft(""),
        {},
        fake,
    )
    assert result.llm_calls == 0  # an empty draft: no call, nothing refused
    assert fake.spent == 0


def test_the_estimate_adds_the_judges_at_their_largest(tmp_path: Path) -> None:
    from hub.graph.pipeline import Pipeline

    pipeline, cache = _pipeline(tmp_path, cache=True)
    assert isinstance(pipeline, Pipeline)
    tickets = _tickets()[:2]
    evidence = _evidence(tickets, render_kb(_sources()))
    without = estimate_cost(pipeline, tickets, evidence, cache)
    with_judges = estimate_cost(pipeline, tickets, evidence, cache, JUDGES)
    per_ticket = 1 + ESTIMATED_CITATIONS + 1  # faithfulness, citations, key facts
    assert with_judges.calls == without.calls + 2 * per_ticket
    assert with_judges.cost_usd > without.cost_usd
    sources = EstimateEvidence.of(b0_evidence(_sources()), _sources())
    requests = judge_requests(pipeline.gateway, JUDGES, tickets[0], sources, 400)
    assert {request.model for request in requests} == {"anthropic/claude-haiku-5-5"}
    assert all(request.max_tokens == 800 for request in requests)
    no_facts = tickets[0].model_copy(update={"key_facts": []})
    assert len(judge_requests(pipeline.gateway, JUDGES, no_facts, sources, 400)) == 4


def test_a_rag_estimate_adds_the_verify_call_and_uses_the_tickets_chunks(tmp_path: Path) -> None:
    from hub.graph.draft import rag_evidence
    from hub.graph.pipeline import Pipeline

    pipeline, cache = _pipeline(tmp_path, cache=True, variant="rag_hybrid")
    assert isinstance(pipeline, Pipeline)
    assert pipeline.verify_prompt is not None
    tickets = _tickets()[:2]
    hits = [_chunk("withdrawal-times#0", "SEPA takes 1 business day.", 1)]
    evidence = {t.id: EstimateEvidence.of(rag_evidence("rag_hybrid", hits)) for t in tickets}
    assert evidence[tickets[0].id].sources == (render_chunks(hits),)
    estimate = estimate_cost(pipeline, tickets, evidence, cache)
    assert (estimate.calls, estimate.cached_calls) == (6, 0)  # triage, write, verify
    with_judges = estimate_cost(pipeline, tickets, evidence, cache, JUDGES)
    assert with_judges.calls == 6 + 2 * (1 + ESTIMATED_CITATIONS + 1)
    longer = {t.id: EstimateEvidence.of(rag_evidence("rag_hybrid", hits * 6)) for t in tickets}
    assert estimate_cost(pipeline, tickets, longer, cache).cost_usd > estimate.cost_usd


def test_no_judges_option() -> None:
    assert parse_args([]).judges
    assert not parse_args(["--no-judges"]).judges


def test_the_judges_are_part_of_the_run_fingerprint() -> None:
    assert files.config_differences(
        _config(judge_prompt_versions={"judge_key_facts": "v1"}), _config()
    ) == ["judge_prompt_versions"]


def test_a_result_pays_for_its_answer_and_its_judges() -> None:
    result = _result("T-0001", paid="0.002").model_copy(
        update={"judges": files.Judgements(paid_cost_usd=Decimal("0.001"))}
    )
    assert result.paid_total_usd == Decimal("0.003")
    assert _result("T-0001", paid="0.002").paid_total_usd == Decimal("0.002")


# --- account tools (P7-06) ----------------------------------------------------------------------


def _tool_settings(**changes: Any) -> Settings:  # noqa: ANN401
    values: dict[str, Any] = {
        "llm_mode": "real",
        "mcp_server_token": "t",  # a placeholder, never sent
        **FIXTURE_CONFIG,
        **changes,
    }
    return Settings(_env_file=None, **values)


def test_a_run_with_account_tools_needs_a_fixed_anchor_date(tmp_path: Path) -> None:
    """P4-06 owner decision: the account facts depend on "today" of the core mock."""
    database = connect("postgresql+psycopg://nobody@localhost:1/none")  # never connected
    with pytest.raises(EvalSetupError, match="CORE_ANCHOR_DATE"):
        build_pipeline(_tool_settings(), database, variant="rag_hybrid", cache_dir=None)
    pipeline, _, _ = build_pipeline(
        _tool_settings(core_anchor_date="2026-11-12"),
        database,
        variant="rag_hybrid",
        cache_dir=None,
    )
    assert pipeline.researches
    assert pipeline.prompt_versions["research"] == "v2"
    # B0 runs no research, so it needs no tools and no date.
    b0, _, _ = build_pipeline(_tool_settings(), database, variant="b0_full_kb", cache_dir=None)
    assert b0.tools is None
    # An explicit no-tools run needs neither the token nor the date.
    plain, _ = _pipeline(tmp_path, cache=False, variant="rag_hybrid")
    assert not plain.researches  # type: ignore[attr-defined]


def test_an_empty_token_stops_the_run_unless_it_is_marked_no_tools(tmp_path: Path) -> None:
    """Owner decision (P7-06): a run without account tools must say so."""
    database = connect("postgresql+psycopg://nobody@localhost:1/none")  # never connected
    settings = _tool_settings(mcp_server_token="", core_anchor_date="2026-11-12")
    with pytest.raises(EvalSetupError, match="--no-tools"):
        build_pipeline(settings, database, variant="rag_hybrid", cache_dir=None)
    pipeline, _, _ = build_pipeline(
        settings, database, variant="rag_hybrid", cache_dir=None, tools=False
    )
    assert pipeline.tools is None
    assert not parse_args(["--no-tools"]).tools
    assert parse_args([]).tools
    assert files.config_differences(_config(account_tools=True), _config()) == ["account_tools"]


def test_the_report_states_a_run_without_tools() -> None:
    data = reporting.evaluate([], [], planned=0)
    without = reporting.render_report(_config(), data, run_name="r")
    assert "| Account tools | **no** (run without tools) |" in without
    with_tools = reporting.render_report(_config(account_tools=True), data, run_name="r")
    assert "| Account tools | yes |" in with_tools


def test_the_estimate_adds_the_research_turns_and_the_account_facts(tmp_path: Path) -> None:
    from hub.graph.draft import rag_evidence

    database = connect("postgresql+psycopg://nobody@localhost:1/none")  # never connected
    settings = _tool_settings(core_anchor_date="2026-11-12")
    pipeline, _, cache = build_pipeline(
        settings, database, variant="rag_hybrid", cache_dir=tmp_path
    )
    plain, _ = _pipeline(tmp_path, cache=True, variant="rag_hybrid")
    tickets = _tickets()[:2]
    hits = [_chunk("withdrawal-times#0", "SEPA takes 1 business day.", 1)]
    evidence = {t.id: EstimateEvidence.of(rag_evidence("rag_hybrid", hits)) for t in tickets}
    with_tools = estimate_cost(pipeline, tickets, evidence, cache)
    without = estimate_cost(plain, tickets, evidence, cache)  # type: ignore[arg-type]
    assert with_tools.calls == without.calls + 2 * RESEARCH_TURNS
    assert with_tools.cost_usd > without.cost_usd
