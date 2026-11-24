"""Memory between tickets (P6-05, ai-pipeline §9): the repeat-contact rule, the transcript the
memory model reads, and the cleaning of its answer. Writing and loading against Postgres are in
tests/integration/test_memory.py."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from hub.db.models import Message
from hub.domain.enums import Intent, MessageRole, TicketStatus
from hub.domain.memory import (
    REPEAT_WINDOW,
    CustomerContext,
    MemoryItem,
    MemorySummary,
    OpenTicket,
    repeat_contacts,
    shows_open_case,
)
from hub.domain.triage import TriageResult
from hub.graph.nodes.decide import open_case
from hub.graph.state import TicketState
from hub.guardrails.placeholders import redact_placeholders
from hub.memory import summary as memory_summary
from hub.memory.summary import MaskFailedError, clean, transcript

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
TEST_CARD = "4111 1111 1111 1111"  # public test card number


def _item(intent: Intent | None, age: timedelta) -> MemoryItem:
    return MemoryItem(
        ticket_id=str(uuid.uuid4()),
        intent=intent,
        summary="A SEPA withdrawal had not arrived; support explained the times.",
        created_at=NOW - age,
    )


def _open(intent: Intent | None) -> OpenTicket:
    return OpenTicket(ticket_id=str(uuid.uuid4()), intent=intent, status=TicketStatus.ANSWERED)


@pytest.mark.parametrize(
    ("context", "expected"),
    [
        (None, False),
        (CustomerContext(), False),
        (CustomerContext(open_tickets=[_open(Intent.WITHDRAWAL_ISSUE)]), True),
        (CustomerContext(open_tickets=[_open(Intent.FEES_FX), _open(None)]), False),
        (CustomerContext(memory=[_item(Intent.WITHDRAWAL_ISSUE, timedelta(days=3))]), True),
        (CustomerContext(memory=[_item(Intent.WITHDRAWAL_ISSUE, REPEAT_WINDOW)]), True),
        (
            CustomerContext(
                memory=[_item(Intent.WITHDRAWAL_ISSUE, REPEAT_WINDOW + timedelta(seconds=1))]
            ),
            False,
        ),
        (CustomerContext(memory=[_item(Intent.CARD_DECLINED, timedelta(days=1))]), False),
    ],
)
def test_a_repeat_contact_on_the_same_intent_shows_an_open_case(
    context: CustomerContext | None, expected: bool
) -> None:
    """ai-pipeline §9: another open ticket with the same intent, or a memory item of the last
    14 days on it."""
    assert shows_open_case(context, Intent.WITHDRAWAL_ISSUE, NOW) is expected


def test_the_repeat_contacts_are_the_open_tickets_then_the_memory_tickets_once_each() -> None:
    """P12: the console links them under "Why you see this ticket"."""
    shared = _open(Intent.WITHDRAWAL_ISSUE)
    recent = _item(Intent.WITHDRAWAL_ISSUE, timedelta(days=2)).model_copy(
        update={"ticket_id": shared.ticket_id}
    )
    other = _item(Intent.WITHDRAWAL_ISSUE, timedelta(days=1))
    context = CustomerContext(
        open_tickets=[_open(Intent.FEES_FX), shared],
        memory=[recent, other, _item(Intent.WITHDRAWAL_ISSUE, REPEAT_WINDOW * 2)],
    )
    assert repeat_contacts(context, Intent.WITHDRAWAL_ISSUE, NOW) == [
        shared.ticket_id,
        other.ticket_id,
    ]
    assert repeat_contacts(context, None, NOW) == []
    assert repeat_contacts(None, Intent.WITHDRAWAL_ISSUE, NOW) == []


def _triage(intent: str) -> TriageResult:
    return TriageResult(
        intent=intent,
        secondary_intent=None,
        urgency="normal",
        wants_human=False,
        reports_unauthorised_activity=False,
        is_complaint_or_legal=False,
        asks_for_action="none",
        injection_suspected=False,
        summary="Asks about a withdrawal.",
    )


def test_decide_reads_the_open_case_from_the_triage_intent_and_the_context() -> None:
    context = CustomerContext(open_tickets=[_open(Intent.WITHDRAWAL_ISSUE)])
    state = TicketState(triage=_triage("withdrawal_issue"), customer_context=context)
    assert open_case(state, NOW)
    assert not open_case(TicketState(triage=_triage("fees_fx"), customer_context=context), NOW)
    assert not open_case(TicketState(customer_context=context), NOW)  # no triage
    assert not open_case(TicketState(triage=_triage("withdrawal_issue")), NOW)  # no context


def _message(role: MessageRole, body: str) -> Message:
    return Message(id=uuid.uuid4(), ticket_id=uuid.uuid4(), role=role, body=body)


def test_the_transcript_has_the_last_ten_messages_with_one_placeholder_mapping() -> None:
    messages = [_message(MessageRole.CUSTOMER, f"message {n}") for n in range(12)]
    messages += [
        _message(MessageRole.CUSTOMER, "Write to ana@example.com please."),
        _message(MessageRole.OPERATOR, "We wrote to ana@example.com and bo@example.com."),
    ]
    text = transcript(messages)
    assert text.count("\n\n") == 9
    assert "message 3" not in text
    assert "message 4" in text
    assert "customer: Write to <EMAIL_1> please." in text
    assert "operator: We wrote to <EMAIL_1> and <EMAIL_2>." in text
    assert "@" not in text


def test_the_transcript_holds_only_what_the_customer_saw() -> None:
    """Owner decision (PR #38 review): customer messages and the replies sent to the customer;
    never `system` messages, enforced in code."""
    messages = [
        _message(MessageRole.CUSTOMER, "Why is my account blocked?"),
        _message(MessageRole.SYSTEM, "Handed off: restricted_account (AML review)."),
        _message(MessageRole.OPERATOR, "We are looking into your account."),
        _message(MessageRole.ASSISTANT, "Thanks for waiting."),
    ]
    text = transcript(messages)
    assert text == (
        "customer: Why is my account blocked?\n\n"
        "operator: We are looking into your account.\n\n"
        "assistant: Thanks for waiting."
    )
    assert transcript([_message(MessageRole.SYSTEM, "Only a notice.")]) == ""


def test_a_summary_keeps_no_placeholder_and_no_card_number() -> None:
    content, masked = clean(f"Asked about card {TEST_CARD}; reply sent to <EMAIL_1>.")
    assert content == "Asked about card [CARD ****1111]; reply sent to [REDACTED]."
    assert masked == {"PAN": 1}


def test_a_summary_whose_mask_fails_is_not_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    class Failed:
        text = ""
        counts: dict[str, int] = {}  # noqa: RUF012 - a test double
        failed = True

    monkeypatch.setattr(memory_summary, "mask", lambda text: Failed())
    with pytest.raises(MaskFailedError):
        clean("anything")


def test_redact_placeholders_replaces_only_ours() -> None:
    text = "Mail <EMAIL_1>, call <PHONE_12>, IBAN <IBAN_2>; typed &lt;EMAIL_1&gt; and <NAME_1>."
    assert redact_placeholders(text) == (
        "Mail [REDACTED], call [REDACTED], IBAN [REDACTED]; typed &lt;EMAIL_1&gt; and <NAME_1>."
    )


@pytest.mark.parametrize("summary", ["", "x" * 301])
def test_the_model_summary_is_short(summary: str) -> None:
    with pytest.raises(ValidationError):
        MemorySummary(summary=summary)


def test_the_memory_prompt_forbids_card_data_and_restriction_reasons() -> None:
    from hub.llm.prompts import active_prompt

    from ._llm import ROOT, real_models

    prompt = active_prompt(ROOT / "prompts", real_models(), "memory_summary")
    assert prompt.front.role == "triage"
    system = prompt.render({"ticket": "customer: hi"})[0]["content"]
    assert isinstance(system, str)
    assert "card numbers" in system
    assert "restricted" in system
