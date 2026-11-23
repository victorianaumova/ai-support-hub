"""The workflow graph (P6-01): its shape against ai-pipeline §1, the node wrapper (step events,
timeouts, failures into a handoff), the state and its checkpoint serialisation, and every node
that needs no database, each alone with a scripted gateway. The nodes that write to Postgres
(intake, triage, handoff_prep, human_review, respond) are tested alone in
tests/integration/test_graph_nodes.py."""

import asyncio
import contextlib
import dataclasses
import re
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast, get_type_hints

import pytest
from langgraph.errors import GraphInterrupt
from langgraph.runtime import Runtime

from hub.domain.draft import Citation, DraftAnswer
from hub.domain.enums import SettingKey
from hub.domain.review import ReviewDecision
from hub.domain.triage import TriageResult
from hub.domain.verification import ClaimCheck, ClaimsAnswer, Verification
from hub.graph import build
from hub.graph import draft as draft_module
from hub.graph import run as run_module
from hub.graph.checkpoint import CheckpointStore, serializer
from hub.graph.context import GraphContext, error_class, failure_reasons
from hub.graph.nodes import act, decide, handoff_prep, policy_gate, research, verify, write
from hub.graph.pipeline import (
    B0_VARIANT,
    VARIANT,
    KbUnavailableError,
    LiveRuns,
    Pipeline,
    write_prompt_for,
)
from hub.graph.recover import STUCK_AFTER_S
from hub.graph.run import RunIncompleteError, outcome_status
from hub.graph.state import STATE_TYPES, Evidence, InputFlags, RouteName, TicketState, run_input
from hub.guardrails.injection import InjectionResult
from hub.guardrails.masking import MaskResult
from hub.guardrails.output_checks import InternalText
from hub.kb.retriever import KbChunk, SearchMode, SearchVisibility
from hub.llm.prompts import active_prompt
from hub.llm.schemas import BudgetExceededError, LlmUnavailableError, StructuredOutputError

from ._llm import (
    CONFIDENCE_RULE,
    POLICY,
    ROOT,
    MemoryRecorder,
    ScriptedProvider,
    make_gateway,
    ok,
    real_models,
)

HAIKU = "anthropic/claude-haiku-5-5"
PROMPTS = ROOT / "prompts"
TICKET_ID = "0196a000-0000-7000-8000-000000000001"
RUN_ID = "0196a000-0000-7000-8000-000000000002"


def _hit(chunk_id: str, rank: int, *, visibility: str = "external") -> KbChunk:
    doc_id = chunk_id.partition("#")[0]
    return KbChunk(
        chunk_id=chunk_id,
        doc_id=doc_id,
        title=f"Title of {doc_id}",
        section=f"Section {rank}",
        url=f"/help/{doc_id}#section-{rank}",
        text=f"A SEPA withdrawal takes 1 business day. Text of {chunk_id}.",
        visibility=visibility,
        score=round(1.0 / rank, 4),
        rank=rank,
    )


HITS = [
    _hit("withdrawal-times#0", 1),
    _hit("internal-escalation-procedures#0", 2, visibility="internal"),
    _hit("withdrawal-fees#1", 3),
]


class FakeSearch:
    def __init__(self, hits: list[KbChunk] | Exception) -> None:
        self.hits = hits
        self.queries: list[tuple[str, str, int, str]] = []

    async def search(
        self,
        query: str,
        visibility: SearchVisibility,
        top_k: int = 6,
        mode: SearchMode = "hybrid",
    ) -> list[KbChunk]:
        self.queries.append((query, visibility, top_k, mode))
        if isinstance(self.hits, Exception):
            raise self.hits
        return list(self.hits)


class NoBudget:
    async def check(self) -> None:
        return None


def _pipeline(
    provider: ScriptedProvider | None = None,
    *,
    variant: str = VARIANT,
    retriever: FakeSearch | None = None,
    recorder: MemoryRecorder | None = None,
    auto_reply_allowed: bool = True,
) -> Pipeline:
    models = real_models()
    return Pipeline(
        database=cast(Any, None),
        gateway=make_gateway(provider or ScriptedProvider({}), recorder),
        budget=NoBudget(),
        triage_prompt=active_prompt(PROMPTS, models, "triage"),
        write_prompt=write_prompt_for(PROMPTS, models, variant),
        policy=POLICY,
        confidence_rule=CONFIDENCE_RULE,
        verify_prompt=active_prompt(PROMPTS, models, "verify") if variant != B0_VARIANT else None,
        variant=variant,
        retriever=retriever,
        auto_reply_allowed=auto_reply_allowed,
    )


class Events:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, name: str, data: dict[str, Any]) -> None:
        self.items.append((name, data))


def _ctx(pipeline: Pipeline | None = None) -> GraphContext:
    return GraphContext(pipeline=pipeline or _pipeline(), emit=Events())


def _state(**changes: Any) -> TicketState:  # noqa: ANN401 - any field value
    state = run_input(ticket_id=TICKET_ID, run_id=RUN_ID, message_id=RUN_ID, customer_ref=RUN_ID)
    state.update(
        TicketState(
            message_masked="How long does a SEPA withdrawal take?",
            message_for_llm="How long does a SEPA withdrawal take?",
            history_for_llm="(no earlier messages)",
            input_flags=InputFlags(
                injection=InjectionResult(score=0.0, patterns=[]),
                pii_placeholders={},
                language="en",
            ),
        )
    )
    state.update(cast(TicketState, changes))
    return state


def _answer(**changes: Any) -> DraftAnswer:  # noqa: ANN401 - any field value
    values: dict[str, Any] = {
        "can_answer": True,
        "missing_information": None,
        "answer": "A SEPA withdrawal takes 1 business day [1].",
        "citations": [Citation(marker=1, source_id="withdrawal-times#0")],
        "proposed_actions": [],
    }
    return DraftAnswer(**(values | changes))


def _triage(**changes: Any) -> TriageResult:  # noqa: ANN401 - any field value
    values: dict[str, Any] = {
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
    return TriageResult(**(values | changes))


def _evidence(hits: list[KbChunk]) -> Evidence:
    return research.evidence([hit for hit in hits if hit.visibility == "external"])


# --- the graph's shape ------------------------------------------------------------------------

# Names in the ai-pipeline §1 diagram → node names in the code.
DOC_NAMES = {
    "start": "__start__",
    "finish": "__end__",
    "gate": "policy_gate",
    "review": "human_review",
}


def _doc_edges() -> set[tuple[str, str]]:
    text = (ROOT / "docs" / "ai-pipeline.md").read_text(encoding="utf-8")
    diagram = text.split("```mermaid", 1)[1].split("```", 1)[0]
    edges = set()
    for line in diagram.splitlines():
        # `a["label"] -- "text" --> b["label"]` or `a --> b`: the ids before and after the arrow.
        match = re.match(r"\s*(\w+).*?-->\s*(\w+)", line)
        if match:
            source, target = (DOC_NAMES.get(name, name) for name in match.groups())
            edges.add((source, target))
    return edges


def test_the_graph_generated_from_the_code_matches_ai_pipeline_section_1() -> None:
    drawn = build.compile_graph(None).get_graph()
    edges = {(edge.source, edge.target) for edge in drawn.edges}
    assert edges == _doc_edges()
    assert set(drawn.nodes) == {name for edge in edges for name in edge}


def test_the_mermaid_image_has_every_node() -> None:
    image = build.mermaid()
    for spec in build.NODES:
        assert f"\t{spec.name}({spec.name})" in image


def test_every_node_has_a_timeout_and_the_model_nodes_outlast_one_call() -> None:
    models = real_models()
    assert set(build.NODE_TIMEOUTS_S) == {spec.name for spec in build.NODES}
    assert all(timeout > 0 for timeout in build.NODE_TIMEOUTS_S.values())
    for node in ("triage", "write", "verify"):
        one_call = models.roles[node].timeout_s
        # the gateway's retries and the fallback get a chance; a stuck provider does not hang
        assert one_call < build.NODE_TIMEOUTS_S[node] < 6 * one_call


@pytest.mark.parametrize(
    ("route", "target"),
    [("handoff", "handoff_prep"), (None, "research"), ("approval", "research")],
)
def test_policy_gate_routes_a_handoff_past_research(route: RouteName | None, target: str) -> None:
    assert build.after_policy_gate(_state(route=route)) == target


@pytest.mark.parametrize(
    ("route", "target"),
    [
        ("auto_reply", "respond"),
        ("approval", "human_review"),
        ("handoff", "handoff_prep"),
        (None, "handoff_prep"),
    ],
)
def test_decide_routes_each_route_to_its_node(route: RouteName | None, target: str) -> None:
    assert build.after_decide(_state(route=route)) == target


UNSUPPORTED = Verification(
    claims=[
        ClaimCheck(claim="1 business day", supported=True, source_id="withdrawal-times#0"),
        ClaimCheck(claim="Card 4111 1111 1111 1111 is free", supported=False, source_id=None),
    ],
    groundedness=0.5,
    policy_violations=[],
    passed=False,
)


def test_verify_goes_back_to_research_once_when_the_check_found_unsupported_claims() -> None:
    """P6-04 (ai-pipeline §5): one retry, then the result stands."""
    assert build.after_verify(_state(verification=UNSUPPORTED)) == "research"
    assert build.after_verify(_state(verification=UNSUPPORTED, retry_count=1)) == "decide"


@pytest.mark.parametrize(
    "changes",
    [
        {"verification": None},  # B0, or no draft
        {"verification": UNSUPPORTED.model_copy(update={"claims": UNSUPPORTED.claims[:1]})},
        # a failed code check: a policy violation is not a search problem
        {"verification": verify.failed_checks(["no_citation"])},
        # the LLM check failed: no claims to search for
        {"verification": verify.unavailable(), "verify_error": "TimeoutError"},
        {"verification": UNSUPPORTED, "verify_error": "TimeoutError"},
        {"verification": UNSUPPORTED, "errors": ["write:LlmUnavailableError"]},
    ],
)
def test_verify_goes_on_to_decide_without_a_retry(changes: dict[str, Any]) -> None:
    assert build.after_verify(_state(**changes)) == "decide"


def test_the_stuck_limit_covers_the_retry_path() -> None:
    longest = sum(build.NODE_TIMEOUTS_S.values()) + sum(
        build.NODE_TIMEOUTS_S[name] for name in ("research", "write", "verify")
    )
    assert build.LONGEST_RUN_S == longest < STUCK_AFTER_S


def _stub(name: str, calls: list[str], update: Any = None) -> Any:  # noqa: ANN401
    async def run(state: TicketState, ctx: GraphContext) -> TicketState:
        calls.append(name)
        return update(state) if callable(update) else cast(TicketState, dict(update or {}))

    return run


async def test_the_retry_loop_cannot_run_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole graph with stub nodes: a verifier that never finds the draft supported sends it
    back to research once, and the second verdict stands (route `approval` from `decide`)."""
    calls: list[str] = []
    stubs = {
        "policy_gate": {"route": None},
        "verify": {"verification": UNSUPPORTED},
        "decide": {"route": "approval"},
        # the real retry counting, without the audit record
        "research": lambda state: TicketState(
            retry_count=state.get("retry_count", 0) + bool(research.retry_hints(state))
        ),
    }
    nodes = tuple(
        dataclasses.replace(spec, run=_stub(spec.name, calls, stubs.get(spec.name)))
        for spec in build.NODES
    )
    monkeypatch.setattr(build, "NODES", nodes)
    graph = build.compile_graph(None)

    final = await graph.ainvoke(_state(), context=_ctx())

    assert calls == [
        "intake",
        "triage",
        "policy_gate",
        "research",
        "write",
        "verify",
        "research",
        "write",
        "verify",
        "decide",
        "human_review",
        "act",
        "respond",
    ]
    assert final["retry_count"] == 1


async def test_a_retry_that_times_out_keeps_the_evidence_and_still_counts() -> None:
    first = _evidence(HITS)
    retried = build.research_timed_out(_state(evidence=first, verification=UNSUPPORTED))
    assert retried == {"retry_count": 1}
    assert build.research_timed_out(_state())["evidence"] == research.evidence((), degraded=True)


# --- the node wrapper -------------------------------------------------------------------------


def _runtime(ctx: GraphContext) -> Runtime[GraphContext]:
    return Runtime(context=ctx)


def _spec(run: Any, **changes: Any) -> build.NodeSpec:  # noqa: ANN401 - test doubles
    return build.NodeSpec(**({"name": "probe", "run": run, "timeout_s": 1.0} | changes))


async def test_a_node_sends_its_step_once_and_returns_its_update() -> None:
    async def run(state: TicketState, ctx: GraphContext) -> TicketState:
        return TicketState(retry_count=1)

    ctx = _ctx()
    node = build.guarded(_spec(run, step="writing"))
    assert await node(_state(), runtime=_runtime(ctx)) == {"retry_count": 1}
    await node(_state(), runtime=_runtime(ctx))
    assert cast(Events, ctx.emit).items == [("run.status", {"step": "writing"})]


async def test_a_failing_node_records_its_error_class_instead_of_raising() -> None:
    async def run(state: TicketState, ctx: GraphContext) -> TicketState:
        raise LlmUnavailableError("the provider said: card 4111 1111 1111 1111")

    node = build.guarded(_spec(run, name="write"))
    update = await node(_state(errors=["intake:Old"]), runtime=_runtime(_ctx()))
    # Skipped: an earlier node failed.
    assert update == {}
    update = await node(_state(), runtime=_runtime(_ctx()))
    # The class only: an error message can quote customer text.
    assert update == {"errors": ["write:LlmUnavailableError"]}


async def test_a_node_that_runs_too_long_fails_with_a_timeout() -> None:
    async def run(state: TicketState, ctx: GraphContext) -> TicketState:
        await asyncio.sleep(5)
        raise AssertionError("the timeout should have stopped this node")

    node = build.guarded(_spec(run, name="triage", timeout_s=0.01))
    assert await node(_state(), runtime=_runtime(_ctx())) == {"errors": ["triage:TimeoutError"]}


async def test_the_nodes_after_the_routing_raise_and_run_after_a_failure() -> None:
    calls: list[str] = []

    async def run(state: TicketState, ctx: GraphContext) -> TicketState:
        calls.append("ran")
        raise RuntimeError("database down")

    node = build.guarded(_spec(run, catches=False, skipped_after_error=False))
    with pytest.raises(RuntimeError):
        await node(_state(errors=["write:X"]), runtime=_runtime(_ctx()))
    assert calls == ["ran"]


async def test_langgraphs_own_control_flow_is_never_taken_for_a_failure() -> None:
    async def run(state: TicketState, ctx: GraphContext) -> TicketState:
        raise GraphInterrupt(())

    node = build.guarded(_spec(run))
    with pytest.raises(GraphInterrupt):
        await node(_state(), runtime=_runtime(_ctx()))


async def test_every_node_that_runs_is_timed_with_its_outcome() -> None:
    """P9-01: the steps the console's Run tab shows (`runs.steps`)."""

    async def fine(state: TicketState, ctx: GraphContext) -> TicketState:
        return TicketState()

    async def broken(state: TicketState, ctx: GraphContext) -> TicketState:
        raise RuntimeError("down")

    async def slow(state: TicketState, ctx: GraphContext) -> TicketState:
        await asyncio.sleep(5)
        raise AssertionError("the timeout should have stopped this node")

    async def pause(state: TicketState, ctx: GraphContext) -> TicketState:
        raise GraphInterrupt(())

    ctx = _ctx()
    runtime = _runtime(ctx)
    await build.guarded(_spec(fine, name="intake"))(_state(), runtime=runtime)
    await build.guarded(_spec(broken, name="triage"))(_state(), runtime=runtime)
    # Skipped after the failure: not a step.
    await build.guarded(_spec(fine, name="write"))(_state(errors=["triage:X"]), runtime=runtime)
    timeout = build.guarded(
        _spec(slow, name="research", timeout_s=0.01, on_timeout=build.research_timed_out)
    )
    await timeout(_state(), runtime=runtime)
    with pytest.raises(GraphInterrupt):
        await build.guarded(_spec(pause, name="human_review"))(_state(), runtime=runtime)
    with pytest.raises(RuntimeError):
        await build.guarded(_spec(broken, name="act", catches=False))(_state(), runtime=runtime)

    assert [(step.node, step.outcome) for step in ctx.steps] == [
        ("intake", "ok"),
        ("triage", "error"),
        ("research", "timeout"),
        ("human_review", "paused"),
        ("act", "error"),
    ]
    assert all(step.duration_ms >= 0 for step in ctx.steps)
    assert ctx.steps[2].duration_ms >= 10
    stored = ctx.steps[0].stored()
    assert set(stored) == {"node", "started_at", "duration_ms", "outcome"}
    assert datetime.fromisoformat(str(stored["started_at"])).tzinfo is not None


def test_the_trace_shows_the_outcome_of_an_invoke_without_text() -> None:
    final = {
        "route": "approval",
        "route_reasons": ["low_confidence"],
        "confidence": 0.6,
        "ticket_status": "pending_approval",
        "errors": [],
        "message_for_llm": "customer text",
        "__interrupt__": (),
    }
    summary = run_module.run_summary(final)
    assert summary["paused"] is True
    assert summary["route"] == "approval"
    assert "customer text" not in str(summary)
    assert run_module.run_summary(None) == {}


def test_which_nodes_fail_into_a_handoff() -> None:
    specs = {spec.name: spec for spec in build.NODES}
    catching = {name for name, spec in specs.items() if spec.catches}
    assert catching == {"intake", "triage", "policy_gate", "research", "write", "verify", "decide"}
    skipped = {name for name, spec in specs.items() if spec.skipped_after_error}
    assert skipped == {"intake", "triage", "research", "write", "verify"}


# --- the context ------------------------------------------------------------------------------


async def test_the_stream_shows_each_step_once_and_b0_never_searches_or_checks() -> None:
    rag = _ctx()
    for step in ("understanding", "understanding", "searching", "writing", "checking"):
        await rag.status(step)
    assert [data["step"] for _, data in cast(Events, rag.emit).items] == [
        "understanding",
        "searching",
        "writing",
        "checking",
    ]
    b0 = _ctx(_pipeline(variant=B0_VARIANT))
    b0.last_step = "understanding"
    for step in ("understanding", "searching", "writing", "checking"):
        await b0.status(step)
    assert [data["step"] for _, data in cast(Events, b0.emit).items] == ["writing"]


def test_a_budget_stop_is_its_own_reason_and_runs_keep_the_first_error_class() -> None:
    assert failure_reasons(["write:LlmUnavailableError"]) == ["system_error"]
    assert failure_reasons(["triage:BudgetExceededError"]) == ["budget_exceeded"]
    assert failure_reasons([]) == ["system_error"]
    assert error_class(["triage:TimeoutError", "write:X"]) == "TimeoutError"
    assert error_class([]) is None


# --- the state ----------------------------------------------------------------------------------


def test_a_new_run_resets_every_field_a_node_writes() -> None:
    fields = set(get_type_hints(TicketState))
    state = run_input(ticket_id="t", run_id="r", message_id="m", customer_ref="c")
    assert set(state) == fields
    assert state["errors"] == []
    assert state["draft"] is None
    assert state["route"] is None


def test_the_state_survives_the_checkpoint_serializer() -> None:
    triage = TriageResult(
        intent="withdrawal_issue",
        secondary_intent=None,
        urgency="normal",
        wants_human=False,
        reports_unauthorised_activity=False,
        is_complaint_or_legal=False,
        asks_for_action="none",
        injection_suspected=False,
        summary="Asks how long a SEPA withdrawal takes.",
    )
    verification = Verification(
        claims=[ClaimCheck(claim="1 day", supported=True, source_id="withdrawal-times#0")],
        groundedness=1.0,
        policy_violations=[],
        passed=True,
    )
    values: list[Any] = [
        _state()["input_flags"],
        _evidence(HITS),
        triage,
        _answer(),
        verification,
        ReviewDecision(kind="edit", operator_id="o", body="Edited.", message_id="m"),
    ]
    codec = serializer()
    for value in values:
        assert codec.loads_typed(codec.dumps_typed(value)) == value
    assert {type(value) for value in values} <= set(STATE_TYPES)


def test_the_serializer_never_rebuilds_a_class_outside_the_state_types() -> None:
    from pydantic import BaseModel

    class Planted(BaseModel):
        command: str = "rm -rf /"

    codec = serializer()
    back = codec.loads_typed(codec.dumps_typed(Planted()))
    assert not isinstance(back, Planted)


def test_a_review_decision_is_built_by_the_api_and_refuses_anything_else() -> None:
    """ai-pipeline §5: the resume value is the decision and nothing else (T15)."""
    with pytest.raises(ValueError, match="extra"):
        ReviewDecision.model_validate({"kind": "approve", "operator_id": "o", "route": "x"})
    with pytest.raises(ValueError, match="kind"):
        ReviewDecision.model_validate({"kind": "auto_reply", "operator_id": "o"})


def test_a_run_is_live_once_per_process() -> None:

    live, run_id = LiveRuns(), uuid.uuid4()
    assert live.claim(run_id)
    assert not live.claim(run_id)
    assert run_id in live
    live.release(run_id)
    assert run_id not in live
    live.release(run_id)  # releasing twice is harmless


def test_a_run_counts_as_stuck_only_after_the_longest_possible_run() -> None:
    """Owner decision (P6-03): longer than every node at its timeout, one after the other, so
    a live run on the other instance is never recovered."""
    assert sum(build.NODE_TIMEOUTS_S.values()) < STUCK_AFTER_S


async def test_a_run_that_ends_without_a_status_is_incomplete() -> None:
    pipeline = cast(Pipeline, None)
    ticket_id = uuid.uuid4()
    with pytest.raises(RunIncompleteError):
        await outcome_status(pipeline, ticket_id, None)
    with pytest.raises(RunIncompleteError):
        await outcome_status(pipeline, ticket_id, {"ticket_status": None})
    assert await outcome_status(pipeline, ticket_id, {"ticket_status": "answered"}) == "answered"


async def test_the_memory_store_compiles_the_graph_once() -> None:
    store = CheckpointStore.memory()
    compiled: list[object] = []

    def compile_graph(saver: object) -> object:
        compiled.append(saver)
        return object()

    first = await store.graph(compile_graph)
    assert await store.graph(compile_graph) is first
    assert len(compiled) == 1
    await store.close()


# --- policy_gate ----------------------------------------------------------------------------


async def test_policy_gate_hands_off_after_a_failed_intake_or_triage() -> None:
    update = await policy_gate.run(_state(errors=["triage:LlmUnavailableError"]), _ctx())
    assert update == {"route": "handoff", "route_reasons": ["system_error"]}
    update = await policy_gate.run(_state(errors=["triage:BudgetExceededError"]), _ctx())
    assert update == {"route": "handoff", "route_reasons": ["budget_exceeded"]}


async def test_policy_gate_lets_a_ticket_no_hard_rule_matches_continue() -> None:
    assert await policy_gate.run(_state(triage=_triage()), _ctx()) == {}


async def test_policy_gate_hands_off_on_a_hard_rule_with_every_matching_reason() -> None:
    triage = _triage(intent="card_lost_stolen", wants_human=True)
    update = await policy_gate.run(_state(triage=triage), _ctx())
    assert update == {"route": "handoff", "route_reasons": ["policy_intent", "customer_request"]}


async def test_policy_gate_hands_off_without_a_triage_result() -> None:
    update = await policy_gate.run(_state(), _ctx())
    assert update == {"route": "handoff", "route_reasons": ["system_error"]}


# --- research -------------------------------------------------------------------------------


async def test_research_searches_with_the_masked_ticket_text_and_keeps_external_chunks() -> None:
    search = FakeSearch(HITS)
    update = await research.run(_state(), _ctx(_pipeline(retriever=search)))
    assert search.queries == [("How long does a SEPA withdrawal take?", "external", 6, "hybrid")]
    evidence = update["evidence"]
    assert evidence is not None
    assert [chunk.chunk_id for chunk in evidence.chunks] == [
        "withdrawal-times#0",
        "withdrawal-fees#1",
    ]
    assert (evidence.top_score, evidence.score_margin, evidence.degraded) == (1.0, 0.6667, False)


async def test_research_of_the_dense_variant_searches_dense() -> None:
    search = FakeSearch(HITS)
    await research.run(_state(), _ctx(_pipeline(variant="rag_dense", retriever=search)))
    assert search.queries[0][3] == "dense"


@pytest.mark.parametrize("retriever", [None, FakeSearch(ConnectionError("qdrant down"))])
async def test_a_failed_search_continues_with_degraded_empty_evidence(
    retriever: FakeSearch | None,
) -> None:
    update = await research.run(_state(), _ctx(_pipeline(retriever=retriever)))
    assert update["evidence"] == Evidence(chunks=[], top_score=0.0, score_margin=0.0, degraded=True)


async def test_b0_runs_no_search() -> None:
    search = FakeSearch(HITS)
    update = await research.run(_state(), _ctx(_pipeline(variant=B0_VARIANT, retriever=search)))
    assert search.queries == []
    assert update["evidence"] is not None
    assert update["evidence"].chunks == []


class _Sessions:
    @contextlib.asynccontextmanager
    async def begin(self) -> AsyncIterator[object]:
        yield object()


async def test_a_retry_searches_with_the_masked_unsupported_claims_and_adds_new_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records: list[dict[str, Any]] = []

    async def record(session: object, **values: Any) -> None:  # noqa: ANN401
        records.append(values)

    monkeypatch.setattr(research, "record", record)
    more = [_hit("withdrawal-fees#1", 1), _hit("card-fees#0", 2), _hit("card-fees#1", 3)]
    search = FakeSearch(more)
    pipeline = _pipeline(retriever=search)
    object.__setattr__(pipeline, "database", SimpleNamespace(sessions=_Sessions()))
    first = _evidence(HITS)
    state = _state(evidence=first, verification=UNSUPPORTED, retry_count=0)

    update = await research.run(state, _ctx(pipeline))

    # only the unsupported claim, card-masked before it leaves the run
    assert search.queries == [("Card [CARD ****1111] is free", "external", 6, "hybrid")]
    evidence = update["evidence"]
    assert evidence is not None
    assert [chunk.chunk_id for chunk in evidence.chunks] == [
        "withdrawal-times#0",
        "withdrawal-fees#1",
        "card-fees#0",
        "card-fees#1",
    ]
    assert (evidence.top_score, evidence.score_margin) == (first.top_score, first.score_margin)
    assert update["retry_count"] == 1
    [audit] = records
    assert audit["action"] == "research.retry"
    assert audit["details"] == {
        "ticket_id": uuid.UUID(TICKET_ID),
        "unsupported_claims": 1,
        "chunks_added": 2,
    }
    assert "1111" not in str(audit)


async def test_a_failed_retry_search_keeps_the_first_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def record(session: object, **values: Any) -> None:  # noqa: ANN401
        return None

    monkeypatch.setattr(research, "record", record)
    pipeline = _pipeline(retriever=FakeSearch(ConnectionError("qdrant down")))
    object.__setattr__(pipeline, "database", SimpleNamespace(sessions=_Sessions()))
    first = _evidence(HITS)
    update = await research.run(_state(evidence=first, verification=UNSUPPORTED), _ctx(pipeline))
    evidence = update["evidence"]
    assert evidence is not None
    assert [c.chunk_id for c in evidence.chunks] == [c.chunk_id for c in first.chunks]
    assert [c.rank for c in evidence.chunks] == [1, 2]  # renumbered in the merged list
    assert update["retry_count"] == 1


def test_a_retry_adds_at_most_one_search_of_new_hits() -> None:
    first = _evidence(HITS)
    more = [_hit(f"other#{n}", n) for n in range(1, 10)]
    merged = research.merged(first, more)
    assert len(merged.chunks) == len(first.chunks) + 6


def test_one_hit_has_its_score_as_the_margin() -> None:
    assert research.evidence([HITS[0]]).score_margin == 1.0


# --- write ----------------------------------------------------------------------------------


async def test_write_drafts_from_the_research_chunks_and_records_its_model() -> None:
    provider = ScriptedProvider({HAIKU: [ok(_answer().model_dump_json())]})
    recorder = MemoryRecorder()
    state = _state(evidence=_evidence(HITS), models={"triage": HAIKU})
    update = await write.run(state, _ctx(_pipeline(provider, recorder=recorder)))
    assert update == {"draft": _answer(), "models": {"triage": HAIKU, "write": HAIKU}}
    [request] = provider.requests
    user = str(request.messages[-1]["content"])
    assert "Source id: withdrawal-times#0" in user
    assert "internal-escalation-procedures" not in user
    assert str(recorder.rows[0].run_id) == RUN_ID


async def test_write_raises_a_model_failure_for_the_wrapper() -> None:
    provider = ScriptedProvider({HAIKU: [ok("not json")] * 2})
    with pytest.raises(StructuredOutputError):
        await write.run(_state(evidence=_evidence(HITS)), _ctx(_pipeline(provider)))


async def test_b0_write_without_a_kb_in_postgres_cannot_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hub.graph import draft

    class Sessions:
        def __call__(self) -> "Sessions":
            return self

        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *args: object) -> None:
            return None

    async def no_sources(session: object) -> list[object]:
        return []

    monkeypatch.setattr(draft, "load_external_sources", no_sources)
    pipeline = _pipeline(variant=B0_VARIANT)
    object.__setattr__(pipeline, "database", type("Db", (), {"sessions": Sessions()})())
    with pytest.raises(KbUnavailableError):
        await write.run(_state(), _ctx(pipeline))


# --- verify ---------------------------------------------------------------------------------


@pytest.fixture
def no_internal_text(monkeypatch: pytest.MonkeyPatch) -> None:
    async def internal_text(pipeline: Pipeline) -> InternalText:
        return InternalText.build(
            internal=["The escalation desk phone list is kept in Confluence."]
        )

    monkeypatch.setattr(verify, "internal_text", internal_text)


@pytest.mark.usefixtures("no_internal_text")
async def test_verify_checks_the_claims_when_every_code_check_passes() -> None:
    claims = ClaimsAnswer(
        claims=[ClaimCheck(claim="1 business day", supported=True, source_id="withdrawal-times#0")]
    )
    provider = ScriptedProvider({HAIKU: [ok(claims.model_dump_json())]})
    state = _state(evidence=_evidence(HITS), draft=_answer(), models={"write": HAIKU})
    update = await verify.run(state, _ctx(_pipeline(provider)))
    verification = update["verification"]
    assert verification is not None
    assert (verification.passed, verification.groundedness) == (True, 1.0)
    assert update["models"] == {"write": HAIKU, "verify": HAIKU}


@pytest.mark.usefixtures("no_internal_text")
async def test_a_failed_code_check_skips_the_model_call() -> None:
    provider = ScriptedProvider({})
    draft = _answer(citations=[Citation(marker=1, source_id="made-up#9")])
    update = await verify.run(
        _state(evidence=_evidence(HITS), draft=draft), _ctx(_pipeline(provider))
    )
    verification = update["verification"]
    assert verification is not None
    assert verification.groundedness is None
    assert verification.policy_violations
    assert provider.requests == []


@pytest.mark.usefixtures("no_internal_text")
async def test_verify_finds_a_prompt_fragment_and_a_canary_in_the_draft() -> None:
    """G7 code controls of P8-02: the pipeline's own prompts are the fragments."""
    pipeline = _pipeline(ScriptedProvider({}))
    fragment = " ".join(pipeline.write_prompt.system.split()[:12])
    draft = _answer(
        answer=f"A SEPA withdrawal takes 1 business day [1].\n\n{fragment} CANARY-INT-HERON"
    )
    update = await verify.run(_state(evidence=_evidence(HITS), draft=draft), _ctx(pipeline))
    verification = update["verification"]
    assert verification is not None
    assert {"canary", "prompt_fragment"} <= set(verification.policy_violations)


@pytest.mark.usefixtures("no_internal_text")
async def test_a_failed_claim_check_counts_the_draft_as_unsupported() -> None:
    provider = ScriptedProvider({HAIKU: [ok("not json")] * 2})
    update = await verify.run(
        _state(evidence=_evidence(HITS), draft=_answer()), _ctx(_pipeline(provider))
    )
    assert update["verification"] == verify.unavailable()
    assert update["verify_error"] == "StructuredOutputError"


@pytest.mark.usefixtures("no_internal_text")
async def test_a_budget_stop_during_verify_is_not_a_failed_check() -> None:
    class Spent:
        async def check(self) -> None:
            raise BudgetExceededError(Decimal("2"), Decimal("1"))

    pipeline = _pipeline(ScriptedProvider({}))
    gateway = make_gateway(ScriptedProvider({}), budget=Spent())
    object.__setattr__(pipeline, "gateway", gateway)
    with pytest.raises(BudgetExceededError):
        await verify.run(_state(evidence=_evidence(HITS), draft=_answer()), _ctx(pipeline))


async def test_b0_and_a_run_without_a_draft_have_nothing_to_verify() -> None:
    assert await verify.run(_state(draft=_answer()), _ctx(_pipeline(variant=B0_VARIANT))) == {}
    assert await verify.run(_state(), _ctx()) == {}


# --- decide and act -------------------------------------------------------------------------


PASSED = Verification(
    claims=[ClaimCheck(claim="1 business day", supported=True, source_id="withdrawal-times#0")],
    groundedness=1.0,
    policy_violations=[],
    passed=True,
)
AUTO_REPLY_ON = {
    SettingKey.AUTO_REPLY_ENABLED: True,
    SettingKey.CONFIDENCE_THRESHOLD: 0.95,
    SettingKey.INTENT_ALLOWLIST: ["withdrawal_issue"],
}


def _settings(monkeypatch: pytest.MonkeyPatch, values: dict[SettingKey, Any]) -> None:
    async def read(pipeline: Pipeline) -> dict[SettingKey, Any]:
        return dict(values)

    monkeypatch.setattr(decide, "read_settings", read)


def _verified(**changes: Any) -> TicketState:  # noqa: ANN401 - any field value
    values: dict[str, Any] = {
        "triage": _triage(),
        "evidence": _evidence(HITS),
        "draft": _answer(),
        "verification": PASSED,
    }
    return _state(**(values | changes))


async def test_decide_auto_replies_only_when_the_settings_allow_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, AUTO_REPLY_ON)
    update = await decide.run(_verified(), _ctx())
    assert (update["route"], update["route_reasons"], update["confidence"]) == (
        "auto_reply",
        [],
        1.0,
    )
    assert update["features"]["groundedness"] == 1.0

    _settings(monkeypatch, {})  # missing settings: the safe side
    update = await decide.run(_verified(), _ctx())
    assert (update["route"], update["route_reasons"]) == ("approval", ["auto_reply_off"])


async def test_decide_never_auto_replies_while_the_deployment_does_not_allow_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0019 (owner decision, P10-02): with AUTO_REPLY_ALLOWED off, a stored
    `auto_reply_enabled: true` is ignored and the draft goes to an operator."""
    _settings(monkeypatch, AUTO_REPLY_ON)
    update = await decide.run(_verified(), _ctx(_pipeline(auto_reply_allowed=False)))
    assert (update["route"], update["route_reasons"]) == ("approval", ["auto_reply_off"])


def test_the_pipeline_does_not_allow_auto_reply_by_default() -> None:
    defaults = {f.name: f.default for f in dataclasses.fields(Pipeline)}
    assert defaults["auto_reply_allowed"] is False


async def test_decide_sends_an_unverified_draft_to_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, AUTO_REPLY_ON)
    update = await decide.run(_verified(verification=verify.unavailable()), _ctx())
    assert (update["route"], update["route_reasons"]) == ("approval", ["unsupported_claims"])


async def test_decide_hands_off_when_masking_model_text_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P3-05a review, note 3: a failed G1 mask is never auto-replied or left for approval."""
    _settings(monkeypatch, AUTO_REPLY_ON)

    def failed(text: str) -> MaskResult:
        return MaskResult(text="[masked]", counts={}, failed=True)

    monkeypatch.setattr(draft_module, "mask", failed)
    update = await decide.run(_verified(), _ctx())
    assert (update["route"], update["route_reasons"]) == ("handoff", ["masking_failed"])


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.8, 0.8),
        (1, 1.0),
        (True, decide.NEVER),
        ("x", decide.NEVER),
        (0, decide.NEVER),
        (-0.5, decide.NEVER),
        (1.5, decide.NEVER),
    ],
)
def test_a_broken_threshold_setting_never_allows_an_auto_reply(
    value: object, expected: float
) -> None:
    assert decide.threshold(value) == expected


@pytest.mark.parametrize(
    ("errors", "reasons"),
    [
        (["write:StructuredOutputError"], ["system_error"]),
        (["write:BudgetExceededError"], ["budget_exceeded"]),
        (["verify:TimeoutError"], ["system_error"]),
    ],
)
async def test_decide_hands_off_after_a_failed_node(errors: list[str], reasons: list[str]) -> None:
    update = await decide.run(_state(draft=_answer(), errors=errors), _ctx())
    assert (update["route"], update["route_reasons"]) == ("handoff", reasons)
    assert update["confidence"] is None


async def test_act_has_nothing_to_run_before_the_review_decision_exists() -> None:
    assert await act.run(_state(route="approval"), _ctx()) == {}


# --- review round (P6-01) -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("research", {"evidence": research.evidence((), degraded=True)}),
        ("verify", {"verification": verify.unavailable(), "verify_error": "TimeoutError"}),
    ],
)
async def test_research_and_verify_continue_after_a_timeout(
    name: str, expected: dict[str, Any]
) -> None:
    """ai-pipeline §3: research continues degraded, verify treats the draft as unsupported; a
    timeout must not turn either into a handoff."""

    async def run(state: TicketState, ctx: GraphContext) -> TicketState:
        await asyncio.sleep(5)
        raise AssertionError("the timeout should have stopped this node")

    spec = next(spec for spec in build.NODES if spec.name == name)
    node = build.guarded(build.NodeSpec(**{**spec.__dict__, "run": run, "timeout_s": 0.01}))
    assert await node(_state(), runtime=_runtime(_ctx())) == expected


def test_a_handoff_always_has_a_reason_even_when_decide_failed() -> None:
    assert handoff_prep.reasons(_state(errors=["decide:KeyError"])) == ["system_error"]
    assert handoff_prep.reasons(_state(route_reasons=["budget_exceeded"])) == ["budget_exceeded"]
    assert handoff_prep.reasons(_state()) == ["system_error"]
