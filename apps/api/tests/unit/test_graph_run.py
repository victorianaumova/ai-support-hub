"""The run's pure helpers (P3-05 to P5-04, moved into the graph modules by P6-01) and the
recording requests. The run against a database is tested in tests/integration/test_graph_run.py;
the graph and its nodes in test_graph_nodes.py."""

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest

from hub.db.models import Message
from hub.domain.draft import Citation, DraftAnswer, ProposedAction
from hub.domain.enums import MessageRole
from hub.domain.triage import TriageResult
from hub.graph import draft as draft_module
from hub.graph.draft import OutputMask, b0_evidence, draft_from_answer, drop_markers, rag_evidence
from hub.graph.nodes.intake import ChatTurn, history_turns, intake
from hub.graph.nodes.triage import audit_details
from hub.graph.pipeline import (
    B0_VARIANT,
    VARIANT,
    Pipeline,
    RunTasks,
    run_failed_problem,
    write_prompt_for,
)
from hub.guardrails.masking import MaskResult
from hub.kb.full_text import KbSource
from hub.kb.retriever import KbChunk
from hub.llm.gateway import FIXTURES_DIR

from ._llm import CONFIDENCE_RULE, POLICY, ROOT, real_models
from ._run_requests import GENERATED, load_tickets, render, request_specs

SOURCES = b0_evidence(
    [
        KbSource(
            "withdrawal-times", "How long does a withdrawal take?", "/help/withdrawal-times", ()
        ),
        KbSource("withdrawal-fees", "Withdrawal fees", "/help/withdrawal-fees", ()),
    ]
)


def _hit(chunk_id: str, rank: int, *, visibility: str = "external") -> KbChunk:
    doc_id = chunk_id.partition("#")[0]
    return KbChunk(
        chunk_id=chunk_id,
        doc_id=doc_id,
        title=f"Title of {doc_id}",
        section=f"Section {rank}",
        url=f"/help/{doc_id}#section-{rank}",
        text=f"Text of {chunk_id}",
        visibility=visibility,
        score=1.0 / rank,
        rank=rank,
    )


HITS = [
    _hit("withdrawal-times#0", 1),
    _hit("internal-escalation-procedures#0", 2, visibility="internal"),
    _hit("withdrawal-fees#1", 3),
]


def test_the_request_files_match_the_run_tickets_prompts_and_kb() -> None:
    """A prompt, ticket or KB change needs `make llm-requests` and then `make record-llm`."""
    specs = request_specs()
    requests = FIXTURES_DIR / "requests"
    on_disk = {path.name for path in requests.iterdir() if GENERATED.fullmatch(path.name)}
    assert on_disk == set(specs), "run `make llm-requests`"
    for name, spec in specs.items():
        assert (requests / name).read_text(encoding="utf-8") == render(spec), (
            f"{name} is out of date: run `make llm-requests` and `make record-llm`"
        )


def test_the_run_tickets_cover_an_answer_and_no_answer() -> None:
    tickets = load_tickets()
    assert [ticket.id for ticket in tickets] == ["run_01", "run_02"]


def _answer(**changes: Any) -> DraftAnswer:  # noqa: ANN401 - any field value
    values: dict[str, Any] = {
        "can_answer": True,
        "missing_information": None,
        "answer": "It takes 1 business day [1]. Write to <EMAIL_1> if not [2].",
        "citations": [
            Citation(marker=1, source_id="withdrawal-times"),
            Citation(marker=2, source_id="internal-escalation-procedures"),
        ],
        "proposed_actions": [],
    }
    return DraftAnswer(**(values | changes))


def test_a_draft_keeps_known_citations_and_counts_unknown_ones() -> None:
    values, unknown = draft_from_answer(_answer(), SOURCES, OutputMask(intake("ana@example.com")))

    assert unknown == 1
    assert values["citations"] == [
        {
            "marker": 1,
            "source_id": "withdrawal-times",
            "doc_id": "withdrawal-times",
            "title": "How long does a withdrawal take?",
            "url": "/help/withdrawal-times",
        }
    ]
    assert values["evidence"] == {
        "variant": B0_VARIANT,
        "dropped_markers": [2],
        "documents": [
            {
                "doc_id": "withdrawal-times",
                "title": "How long does a withdrawal take?",
                "url": "/help/withdrawal-times",
            }
        ],
    }


def test_a_rag_citation_becomes_a_link_to_the_section_of_its_chunk() -> None:
    """P5-03: the model names a chunk id; title and section URL come from the search hit."""
    answer = _answer(
        answer="It takes 1 business day [1]. Fees apply [2]. Ask us [3].",
        citations=[
            Citation(marker=1, source_id="withdrawal-times#0"),
            Citation(marker=2, source_id="withdrawal-fees#1"),
            Citation(marker=3, source_id="withdrawal-times"),  # a document id is not a source
        ],
    )
    values, unknown = draft_from_answer(
        answer, rag_evidence(VARIANT, HITS), OutputMask(intake("hi"))
    )
    assert unknown == 1
    assert values["citations"] == [
        {
            "marker": 1,
            "source_id": "withdrawal-times#0",
            "doc_id": "withdrawal-times",
            "title": "Title of withdrawal-times",
            "url": "/help/withdrawal-times#section-1",
        },
        {
            "marker": 2,
            "source_id": "withdrawal-fees#1",
            "doc_id": "withdrawal-fees",
            "title": "Title of withdrawal-fees",
            "url": "/help/withdrawal-fees#section-3",
        },
    ]
    assert values["body"] == "It takes 1 business day [1]. Fees apply [2]. Ask us."
    evidence = values["evidence"]
    assert (evidence["variant"], evidence["dropped_markers"]) == (VARIANT, [3])
    assert [chunk["chunk_id"] for chunk in evidence["chunks"]] == [
        "withdrawal-times#0",
        "withdrawal-fees#1",
    ]
    assert evidence["chunks"][0] == {
        "chunk_id": "withdrawal-times#0",
        "doc_id": "withdrawal-times",
        "title": "Title of withdrawal-times",
        "section": "Section 1",
        "url": "/help/withdrawal-times#section-1",
        "score": 1.0,
        "rank": 1,
        "text": "Text of withdrawal-times#0",
    }


def test_an_internal_hit_never_reaches_the_writer_or_a_citation() -> None:
    """AGENTS.md never-do 5: the search filters on `external`; this is the second check."""
    evidence = rag_evidence(VARIANT, HITS)
    assert "internal-escalation-procedures" not in evidence.kb
    assert "internal-escalation-procedures#0" not in evidence.citable
    answer = _answer(
        answer="See the procedure [1].",
        citations=[Citation(marker=1, source_id="internal-escalation-procedures#0")],
    )
    values, unknown = draft_from_answer(answer, evidence, OutputMask(intake("hi")))
    assert (unknown, values["citations"], values["body"]) == (1, [], "See the procedure.")


def test_the_writer_reads_the_hits_in_rank_order_with_their_ids() -> None:
    kb = rag_evidence(VARIANT, HITS).kb
    assert kb.index("Source id: withdrawal-times#0") < kb.index("Source id: withdrawal-fees#1")
    assert "Text of withdrawal-fees#1" in kb


def test_each_variant_runs_its_own_write_prompt() -> None:
    prompts, models = ROOT / "prompts", real_models()
    assert write_prompt_for(prompts, models, B0_VARIANT).version == "write/v1"
    assert write_prompt_for(prompts, models, VARIANT).version == "write/v6"
    assert write_prompt_for(prompts, models, "rag_dense").version == "write/v6"


@pytest.mark.parametrize(("variant", "version"), [(B0_VARIANT, "v2"), (VARIANT, "v1")])
def test_a_pipeline_refuses_a_write_prompt_of_the_other_shape(variant: str, version: str) -> None:
    from hub.llm.prompts import load_prompt

    prompt = load_prompt(ROOT / "prompts", "write", version)
    with pytest.raises(ValueError, match="cannot run"):
        Pipeline(
            database=cast(Any, None),
            gateway=cast(Any, None),
            budget=cast(Any, None),
            triage_prompt=prompt,
            write_prompt=prompt,
            policy=POLICY,
            confidence_rule=CONFIDENCE_RULE,
            variant=variant,
        )


def test_a_draft_gets_the_runs_placeholders_restored() -> None:
    values, _ = draft_from_answer(_answer(), SOURCES, OutputMask(intake("ana@example.com")))
    # [2] cites an internal document: the citation is dropped, and so is its marker (N2).
    assert values["body"] == "It takes 1 business day [1]. Write to ana@example.com if not."
    assert values["evidence"]["dropped_markers"] == [2]


def test_a_draft_that_cannot_answer_has_an_empty_body_and_the_missing_information() -> None:
    answer = _answer(
        can_answer=False,
        answer="some text the model should not have written",
        missing_information="Interest on balances (<EMAIL_1>) is not in the KB.",
        citations=[],
    )
    values, unknown = draft_from_answer(answer, SOURCES, OutputMask(intake("ana@example.com")))

    assert (values["can_answer"], values["body"], unknown) == (False, "", 0)
    assert (
        values["missing_information"] == "Interest on balances (ana@example.com) is not in the KB."
    )


def test_proposed_actions_get_ids_for_the_operator() -> None:
    action = ProposedAction(
        type="jira_create_issue",
        issue_type="dispute",
        summary="Dispute for <EMAIL_1>",
        description="Check the payment.",
    )
    values, _ = draft_from_answer(
        _answer(proposed_actions=[action, action]), SOURCES, OutputMask(intake("ana@example.com"))
    )
    assert [item["id"] for item in values["proposed_actions"]] == ["a1", "a2"]
    assert values["proposed_actions"][0] == {
        "id": "a1",
        "type": "jira_create_issue",
        "params": {
            "issue_type": "dispute",
            "summary": "Dispute for ana@example.com",
            "description": "Check the payment.",
        },
    }


def test_history_is_the_messages_before_the_trigger() -> None:
    def message(role: MessageRole, body: str) -> Message:
        return cast(Message, SimpleNamespace(id=uuid.uuid4(), role=role, body=body))

    first = message(MessageRole.CUSTOMER, "first")
    reply = message(MessageRole.OPERATOR, "reply")
    trigger = message(MessageRole.CUSTOMER, "now")
    later = message(MessageRole.CUSTOMER, "later")

    assert history_turns([first, reply, trigger, later], trigger.id) == [
        ChatTurn(role="customer", text="first"),
        ChatTurn(role="operator", text="reply"),
    ]


def test_the_triage_audit_record_has_the_decision_and_never_the_summary() -> None:
    result = TriageResult(
        intent="withdrawal_issue",
        secondary_intent=None,
        urgency="normal",
        wants_human=False,
        reports_unauthorised_activity=False,
        is_complaint_or_legal=False,
        asks_for_action="none",
        injection_suspected=False,
        summary="Customer ana@example.com asks about a SEPA withdrawal of EUR 420.",
    )
    details = audit_details(result, "triage/v1", "anthropic/claude-haiku-5-5")

    assert details["intent"] == "withdrawal_issue"
    assert details["prompt_version"] == "triage/v1"
    assert "summary" not in details
    assert not any("ana@" in str(value) or "420" in str(value) for value in details.values())


def test_the_error_event_is_problem_details_without_internals() -> None:
    problem = run_failed_problem()
    assert problem["code"] == "internal_error"
    assert problem["status"] == 500
    assert set(problem) == {"type", "title", "status", "detail", "code"}


async def test_run_tasks_wait_for_runs_in_flight() -> None:
    tasks = RunTasks()
    done: list[str] = []

    async def job() -> None:
        await asyncio.sleep(0.01)
        done.append("run")

    tasks.start(job())
    await tasks.wait(timeout_s=1)
    assert done == ["run"]
    await tasks.wait(timeout_s=1)  # nothing in flight: returns at once


# --- output masking (B1, AGENTS.md never-do #1) ------------------------------------------------

TEST_CARD = "4111 1111 1111 1111"  # public test card number


def test_every_model_written_string_of_a_draft_is_card_masked() -> None:
    action = ProposedAction(
        type="jira_create_issue",
        issue_type="dispute",
        summary=f"Dispute card {TEST_CARD}",
        description=f"Customer card {TEST_CARD}, check the payment.",
    )
    answer = _answer(
        answer=f"Your card {TEST_CARD} was charged [1].",
        citations=[Citation(marker=1, source_id="withdrawal-times")],
        proposed_actions=[action],
    )
    clean = OutputMask(intake("hello"))
    values, _ = draft_from_answer(answer, SOURCES, clean)

    assert values["body"] == "Your card [CARD ****1111] was charged [1]."
    params = values["proposed_actions"][0]["params"]
    assert params["summary"] == "Dispute card [CARD ****1111]"
    assert params["description"] == "Customer card [CARD ****1111], check the payment."
    assert clean.counts == {"PAN": 3}
    assert "4111 1111" not in str(values)


def test_missing_information_is_card_masked() -> None:
    answer = _answer(
        can_answer=False, answer="", citations=[], missing_information=f"Card {TEST_CARD}?"
    )
    values, _ = draft_from_answer(answer, SOURCES, OutputMask(intake("hello")))
    assert values["missing_information"] == "Card [CARD ****1111]?"


def test_a_restored_placeholder_is_masked_too() -> None:
    """Restoring happens first, so the mask also sees what the placeholders put back."""
    clean = OutputMask(intake("write to ana@example.com"))
    assert clean(f"Sent to <EMAIL_1> about {TEST_CARD}.") == (
        "Sent to ana@example.com about [CARD ****1111]."
    )


def test_a_failed_mask_is_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        draft_module, "mask", lambda text: MaskResult(text="#" * len(text), counts={}, failed=True)
    )
    clean = OutputMask(intake("hello"))
    assert clean("abc") == "###"
    assert clean.counts == {"failed": 1}


@pytest.mark.parametrize(
    ("body", "keep", "expected", "dropped"),
    [
        ("A [1]. B [2].", {1, 2}, "A [1]. B [2].", []),
        ("A [1]. B [2].", {1}, "A [1]. B.", [2]),
        ("A[3] and [3] again.", set(), "A and again.", [3, 3]),
        ("No markers.", {1}, "No markers.", []),
    ],
)
def test_markers_without_a_kept_citation_are_removed(
    body: str, keep: set[int], expected: str, dropped: list[int]
) -> None:
    assert drop_markers(body, keep) == (expected, dropped)


# --- verify (P5-04) ---------------------------------------------------------------------------


def _pipeline(variant: str, *, verify: bool) -> Pipeline:
    from hub.llm.prompts import active_prompt

    prompts, models = ROOT / "prompts", real_models()
    return Pipeline(
        database=cast(Any, None),
        gateway=cast(Any, None),
        budget=cast(Any, None),
        triage_prompt=active_prompt(prompts, models, "triage"),
        write_prompt=write_prompt_for(prompts, models, variant),
        policy=POLICY,
        confidence_rule=CONFIDENCE_RULE,
        verify_prompt=active_prompt(prompts, models, "verify") if verify else None,
        variant=variant,
    )


def test_a_rag_pipeline_needs_a_verify_prompt_and_b0_has_no_verify_step() -> None:
    with pytest.raises(ValueError, match="needs a verify prompt"):
        _pipeline(VARIANT, verify=False)
    rag = _pipeline(VARIANT, verify=True)
    assert rag.verifies
    assert rag.prompt_versions == {"triage": "v1", "write": "v6", "verify": "v1"}
    b0 = _pipeline(B0_VARIANT, verify=True)
    assert not b0.verifies
    assert b0.prompt_versions == {"triage": "v1", "write": "v1"}


def test_the_code_checks_see_the_answer_as_the_model_wrote_it() -> None:
    from hub.graph.draft import draft_to_check

    evidence = rag_evidence(VARIANT, HITS)
    answer = _answer(
        answer="Write to <EMAIL_1> [1]. Card 4111 1111 1111 1111 [7].",
        citations=[Citation(marker=1, source_id="withdrawal-times#0")],
    )
    checked = draft_to_check(answer, evidence, intake("ana@example.com"))
    # Placeholders restored, card digits and the marker without a citation still there.
    assert checked.answer == "Write to ana@example.com [1]. Card 4111 1111 1111 1111 [7]."
    assert checked.citations == [(1, "withdrawal-times#0")]
    assert checked.evidence_ids == {"withdrawal-times#0", "withdrawal-fees#1"}
    assert checked.other_texts == []


def test_the_code_checks_see_missing_information_and_proposed_actions() -> None:
    from hub.graph.draft import draft_to_check

    answer = _answer(
        can_answer=False,
        answer="",
        citations=[],
        missing_information="Ask <EMAIL_1> for the receipt.",
        proposed_actions=[
            ProposedAction(
                type="jira_create_issue", issue_type="dispute", summary="S", description="D"
            )
        ],
    )
    checked = draft_to_check(answer, rag_evidence(VARIANT, HITS), intake("ana@example.com"))
    assert checked.other_texts == [
        ("missing_information", "Ask ana@example.com for the receipt."),
        ("proposed_action", "S\nD"),
    ]


def test_the_draft_stores_groundedness_and_the_unsupported_claims_masked() -> None:
    from decimal import Decimal

    from hub.domain.verification import ClaimCheck, Verification
    from hub.graph.draft import verification_columns

    claims = [
        ClaimCheck(claim="It takes 1 day", supported=True, source_id="withdrawal-times#0"),
        ClaimCheck(claim="Card 4111 1111 1111 1111 is fine", supported=False, source_id=None),
    ]
    verification = Verification(claims=claims, groundedness=0.5, policy_violations=[], passed=False)
    clean = OutputMask(intake("hi"))
    assert verification_columns(verification, clean) == {
        "groundedness": Decimal("0.5"),
        "unsupported_claims": [{"claim": "Card [CARD ****1111] is fine", "source_id": None}],
    }
    assert verification_columns(None, clean) == {"groundedness": None, "unsupported_claims": []}
    values, _ = draft_from_answer(_answer(), SOURCES, clean, verification)
    assert values["groundedness"] == Decimal("0.5")


def test_the_verify_audit_record_has_counts_and_codes_never_a_claim() -> None:
    from hub.domain.verification import ClaimCheck, Verification
    from hub.graph.draft import verify_audit_details

    claim = ClaimCheck(claim="secret words", supported=False, source_id=None)
    details = verify_audit_details(
        Verification(claims=[claim], groundedness=0.0, policy_violations=[], passed=False)
    )
    assert details == {
        "passed": False,
        "groundedness": 0.0,
        "claims": 1,
        "unsupported_claims": 1,
        "policy_violations": [],
        "llm_check": True,
    }
    assert "secret" not in str(details)
