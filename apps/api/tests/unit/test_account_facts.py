"""Account facts and the customer profile from core tool results (P7-04, `hub.tools.facts`):
only listed fields pass, never a restriction reason; texts are masked; ids are stable. Also the
writer's side: facts are citable, stored with the draft, read by the verifier; memory is context
only; and the profile's restricted flag drives row 1 (`customer_restricted`)."""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from hub.domain.account import AccountFact, CustomerProfile
from hub.domain.draft import Citation, DraftAnswer
from hub.domain.memory import CustomerContext, MemoryItem
from hub.domain.triage import TriageResult
from hub.graph.draft import (
    ACCOUNT_TITLE,
    NO_ACCOUNT_FACTS,
    NO_MEMORY,
    OutputMask,
    draft_from_answer,
    memory_text,
    rag_evidence,
)
from hub.graph.nodes.intake import intake
from hub.graph.nodes.policy_gate import restricted
from hub.graph.nodes.write import write_messages
from hub.graph.pipeline import VARIANT
from hub.graph.routing import gate
from hub.graph.state import InputFlags, TicketState
from hub.guardrails.injection import InjectionResult
from hub.kb.retriever import KbChunk
from hub.llm.prompts import active_prompt
from hub.tools.facts import (
    MAX_ACCOUNT_FACTS,
    add_facts,
    fact_texts,
    profile,
    render_facts,
)

from ._llm import POLICY, ROOT, real_models

VISA = "4111 1111 1111 1111"  # public test card number
REASON = "sanctions screening hit"
PROMPT = active_prompt(ROOT / "prompts", real_models(), "write")

PROFILE: dict[str, Any] = {
    "customer_ref": "CUS-0007",
    "tier": "verified",
    "country": "PT",
    "verification": {
        "current_tier_checks": "complete",
        "upgrade": {
            "target_tier": "business",
            "status": "rejected",
            "submitted_at": "2026-11-01T10:00:00Z",
            "error_code": "KYB-07",
        },
    },
    "restricted": True,
    "restriction_reason": REASON,
    "limits": [
        {"name": "withdraw_per_day", "limit_eur": "10000", "used": {"USD": "5", "EUR": "420"}},
        {"name": "receive_per_month", "limit_eur": "30000", "used": {}},
    ],
    "as_of": "2026-11-12",
}
TX: dict[str, Any] = {
    "reference": "TX-000042",
    "type": "withdrawal_sepa",
    "amount": "420",
    "currency": "EUR",
    "fee": "1",
    "fee_currency": "EUR",
    "status": "failed",
    "error_code": "WD-104",
    "requested_at": "2026-11-09T09:15:00Z",
    "updated_at": "2026-11-10T08:00:00Z",
    "counterparty": "Ana Silva",
    "reason": REASON,
}


def test_the_profile_keeps_tier_country_and_the_flag_never_the_reason() -> None:
    assert profile(PROFILE) == CustomerProfile(tier="verified", country="PT", restricted=True)
    assert REASON not in repr(profile(PROFILE))
    assert profile({"tier": "verified"}) is None


def test_profile_facts_have_tier_upgrade_and_limits_but_no_restriction() -> None:
    texts = fact_texts("core_get_customer", PROFILE)
    assert texts == [
        "Account: tier verified, country PT, checks of the current tier complete.",
        "Upgrade to tier business: status rejected, error code KYB-07, "
        "submitted 2026-11-01 10:00 UTC.",
        "Limit withdraw_per_day: 10000.00 EUR (EUR equivalent); used: 420.00 EUR, 5.00 USD "
        "(as of 2026-11-12).",
        "Limit receive_per_month: 30000.00 EUR (EUR equivalent); used: nothing (as of 2026-11-12).",
    ]
    assert not any("restrict" in text.lower() or REASON in text for text in texts)


def test_transaction_facts_name_status_error_code_and_dates_without_the_counterparty() -> None:
    [text] = fact_texts("core_list_transactions", {"transactions": [TX]}) or []
    assert text == (
        "Transaction TX-000042: withdrawal_sepa, 420.00 EUR, fee 1.00 EUR, status failed, "
        "error code WD-104, requested 2026-11-09 09:15 UTC, last update 2026-11-10 08:00 UTC."
    )
    assert fact_texts("core_list_transactions", {"transactions": []}) == ["No transactions found."]
    detail = {
        **TX,
        "error_code": None,
        "merchant_category": "groceries",
        "status_history": [{"status": "pending", "at": "2026-11-09T09:15:00Z"}],
        "expected_event": "arrival",
        "expected_by": "2026-11-11",
        "overdue": True,
        "card_ref": "CARD-0001",
    }
    [text] = fact_texts("core_get_transaction", detail) or []
    assert "status failed, requested" in text
    assert "Merchant category: groceries." in text
    assert "Status history: pending 2026-11-09 09:15 UTC." in text
    assert text.endswith("Expected arrival by 2026-11-11 (overdue).")
    assert "Ana Silva" not in text
    assert "CARD-0001" not in text


def test_card_facts_and_masking() -> None:
    card = {
        "card_ref": "CARD-0001",
        "kind": "physical",
        "last4": "1111",
        "status": "active",
        "issued_at": "2026-01-05T10:00:00Z",
        "delivery_status": "shipped",
        "delivery_method": "express",
        "expected_delivery_by": "2026-11-14",
        "delivery_overdue": True,
    }
    [text] = fact_texts("core_get_cards", {"cards": [card]}) or []
    assert text == (
        "Card ending 1111 (physical): status active, issued 2026-01-05 10:00 UTC; "
        "delivery shipped (express), expected by 2026-11-14 (overdue)."
    )
    assert fact_texts("core_get_cards", {"cards": []}) == ["No cards."]
    # A field that is passed on is G1-masked.
    leaky = {**TX, "error_code": f"card {VISA}"}
    [masked] = fact_texts("core_list_transactions", {"transactions": [leaky]}) or []
    assert VISA not in masked
    assert "[CARD ****1111]" in masked


@pytest.mark.parametrize(
    ("tool", "result"),
    [
        ("core_get_customer", {"tier": "verified"}),
        ("core_get_transaction", {"reference": "TX-1"}),
        ("jira_create_issue", {"key": "TB-1"}),
    ],
)
def test_an_unreadable_or_unknown_result_gives_no_fact(tool: str, result: dict[str, Any]) -> None:
    assert fact_texts(tool, result) is None


def test_fact_ids_are_stable_deduplicated_and_capped() -> None:
    facts, these = add_facts([], "core_get_cards", ["a", "b"])
    assert [(f.fact_id, f.text) for f in facts] == [("A1", "a"), ("A2", "b")]
    assert these == facts
    facts, these = add_facts(facts, "core_list_transactions", ["b", "c"])
    assert [f.fact_id for f in these] == ["A2", "A3"]
    assert these[0].source_tool == "core_get_cards"
    many = [f"t{n}" for n in range(MAX_ACCOUNT_FACTS + 5)]
    facts, these = add_facts(facts, "core_list_transactions", many)
    assert len(facts) == MAX_ACCOUNT_FACTS
    assert render_facts(facts[:2]) == "Source id: A1\na\n\nSource id: A2\nb"


# --- the writer and the verifier ---------------------------------------------------------------


HIT = KbChunk(
    chunk_id="withdrawal-failed#0",
    doc_id="withdrawal-failed",
    title="Why did my withdrawal fail?",
    section="Error codes",
    url="/help/withdrawal-failed#error-codes",
    text="A failed withdrawal returns to the balance.",
    visibility="external",
    score=1.0,
    rank=1,
)
FACT = AccountFact(
    fact_id="A1", source_tool="core_list_transactions", text="Transaction TX-000042: failed."
)


def test_account_facts_are_citable_and_stored_with_the_draft() -> None:
    evidence = rag_evidence(VARIANT, [HIT], [FACT])
    assert set(evidence.citable) == {"withdrawal-failed#0", "A1"}
    assert evidence.account_facts == "Source id: A1\nTransaction TX-000042: failed."
    assert evidence.sources == f"{evidence.kb}\n\nSource id: A1\nTransaction TX-000042: failed."
    answer = DraftAnswer(
        can_answer=True,
        missing_information=None,
        answer="Your withdrawal TX-000042 failed [1]. It returns to your balance [2].",
        citations=[
            Citation(marker=1, source_id="A1"),
            Citation(marker=2, source_id="withdrawal-failed#0"),
        ],
        proposed_actions=[],
    )
    columns, unknown = draft_from_answer(answer, evidence, OutputMask(intake("hi")))
    assert unknown == 0
    assert columns["citations"][0] == {
        "marker": 1,
        "source_id": "A1",
        "doc_id": "account",
        "title": ACCOUNT_TITLE,
        "url": "",
    }
    assert columns["evidence"]["account_facts"] == [FACT.model_dump(mode="json")]


def test_without_facts_the_writer_and_verifier_get_what_they_got_before() -> None:
    evidence = rag_evidence(VARIANT, [HIT])
    assert evidence.sources == evidence.kb
    assert evidence.account_facts == NO_ACCOUNT_FACTS
    assert "account_facts" not in evidence.stored(set(), [])


def test_write_v4_has_the_facts_and_the_memory_in_the_user_part() -> None:
    assert PROMPT.version == "write/v6"
    memory = CustomerContext(
        memory=[
            MemoryItem(
                ticket_id=str(uuid.uuid4()),
                intent="withdrawal_issue",
                summary="Asked about a slow SEPA withdrawal; it arrived.",
                created_at=datetime(2026, 11, 2, tzinfo=UTC),
            )
        ]
    )
    [system, user] = write_messages(
        PROMPT,
        kb="KB",
        ticket="Where is my withdrawal?",
        history="(none)",
        account_facts=render_facts([FACT]),
        memory=memory_text(memory),
    )
    text = user["content"]
    assert (
        "<account_facts>\nSource id: A1\nTransaction TX-000042: failed.\n</account_facts>" in text
    )
    assert (
        "<memory>\n- 2026-11-02 (withdrawal_issue): Asked about a slow SEPA withdrawal; "
        "it arrived.\n</memory>"
    ) in text
    assert "never a source" in str(system["content"])
    assert memory_text(None) == NO_MEMORY
    assert memory_text(CustomerContext()) == NO_MEMORY


# --- row 1: customer_restricted ------------------------------------------------------------------


def _triage() -> TriageResult:
    return TriageResult(
        intent="withdrawal_issue",
        secondary_intent=None,
        urgency="normal",
        wants_human=False,
        reports_unauthorised_activity=False,
        is_complaint_or_legal=False,
        asks_for_action="none",
        injection_suspected=False,
        summary="Asks where a withdrawal is.",
    )


FLAGS = InputFlags(
    injection=InjectionResult(score=0.0, patterns=[]), pii_placeholders={}, language="en"
)


@pytest.mark.parametrize(
    ("flag", "reasons"),
    [(True, ["restricted_account"]), (False, []), (None, [])],
)
def test_the_profiles_restricted_flag_drives_row_1(flag: bool | None, reasons: list[str]) -> None:
    context = CustomerContext(
        profile=None
        if flag is None
        else CustomerProfile(tier="verified", country="PT", restricted=flag)
    )
    state = TicketState(customer_context=context)
    assert restricted(state) is flag
    decision = gate(
        POLICY,
        errors=[],
        triage=_triage(),
        message_masked="Where is my withdrawal?",
        flags=FLAGS,
        customer_restricted=restricted(state),
    )
    assert decision.reasons == reasons
    assert decision.route == ("handoff" if reasons else None)
    assert restricted(TicketState()) is None
