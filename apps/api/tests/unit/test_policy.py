"""The routing policy (`config/policy.yaml`, `hub.guardrails.policy`; ai-pipeline §6, P6-02):
row 1 rule by rule and their order, rows 6 and 7, the loader, and the handoff notice."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from hub.domain.enums import RouteReason
from hub.domain.triage import TriageResult
from hub.graph.nodes.intake import language
from hub.guardrails import policy as pol
from hub.guardrails.policy import (
    GateInput,
    Policy,
    PolicyError,
    RuleMatch,
    auto_reply_intents,
    handoff_notice,
    hard_rules,
    has_fraud_words,
    is_open,
    is_urgent,
    load_policy,
    needs_review,
    next_opening,
    reasons,
    response_time,
    review_rules,
    safe_first_steps,
)

from ._llm import ROOT

POLICY_YAML = ROOT / "config" / "policy.yaml"
POLICY = load_policy(POLICY_YAML)
FREEZE = "Please freeze your card in the app now. You can freeze it at any time."
NEVER_ASK = "Tallybridge staff never ask for your full card number, CVV, PIN, password or 2FA code."
# Europe/Bratislava: CET (UTC+1) in November, CEST (UTC+2) in July.
WEDNESDAY_NOON = datetime(2026, 11, 18, 11, 0, tzinfo=UTC)  # 12:00 CET, open


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
        "summary": "Asks about a withdrawal.",
    }
    return TriageResult(**(values | changes))


def _gate(**changes: Any) -> GateInput:  # noqa: ANN401 - any field value
    values: dict[str, Any] = {
        "triage": _triage(),
        "message_masked": "My withdrawal of 420 EUR has not arrived.",
        "english": True,
        "pan_found": False,
        "injection_score": 0.0,
        "customer_restricted": None,
    }
    return GateInput(**(values | changes))


def _reasons(given: GateInput) -> list[str]:
    return reasons(hard_rules(POLICY, given))


# --- row 1: every rule alone ------------------------------------------------------------------


def test_a_routine_ticket_matches_no_hard_rule() -> None:
    assert hard_rules(POLICY, _gate()) == []


@pytest.mark.parametrize("intent", ["account_restricted", "card_lost_stolen", "other"])
def test_each_handoff_intent_is_a_policy_intent(intent: str) -> None:
    assert _reasons(_gate(triage=_triage(intent=intent))) == ["policy_intent"]


@pytest.mark.parametrize(
    ("flag", "reason"),
    [
        ("wants_human", "customer_request"),
        ("reports_unauthorised_activity", "fraud_report"),
        ("is_complaint_or_legal", "complaint"),
        ("injection_suspected", "injection"),
    ],
)
def test_each_handoff_flag_has_its_reason(flag: str, reason: str) -> None:
    assert _reasons(_gate(triage=_triage(**{flag: True}))) == [reason]


@pytest.mark.parametrize("action", ["refund", "limit_increase", "unfreeze"])
def test_asking_us_to_do_a_listed_action_is_an_action_request(action: str) -> None:
    assert _reasons(_gate(triage=_triage(asks_for_action=action))) == ["action_request"]


@pytest.mark.parametrize("action", ["none", "close_account", "dispute", "other"])
def test_other_actions_are_not_hard_rules(action: str) -> None:
    assert _reasons(_gate(triage=_triage(asks_for_action=action))) == []


@pytest.mark.parametrize("english", [False, None])
def test_a_message_that_is_not_known_to_be_english_is_handed_off(english: bool | None) -> None:
    """Owner decision (P6-02): an unknown language counts as not English."""
    assert _reasons(_gate(english=english)) == ["language"]


def test_a_restricted_customer_is_handed_off_once_the_profile_is_known() -> None:
    assert _reasons(_gate(customer_restricted=True)) == ["restricted_account"]
    assert _reasons(_gate(customer_restricted=False)) == []


def test_customer_restricted_never_matches_until_the_profile_exists() -> None:
    """The profile comes with core_get_customer (P7-04); until then `customer_restricted` is None
    and the rule does not match."""
    assert _reasons(_gate(customer_restricted=None)) == []


@pytest.mark.parametrize(
    "text",
    [
        "This payment with [CARD ****1111] is fraud.",
        "My card [CARD ****1111] was stolen yesterday",
        "I did not make this payment, card [CARD ****1111]",
        "[CARD ****1111] someone used it in Lisbon",
        "Not me! [CARD ****1111]",
        "My card [CARD ****1111] was HACKED",
        "fr\u0430ud on [CARD ****1111]",  # a Cyrillic a
        "scam with [CARD ****1111]",
        "an unauthorized charge on [CARD ****1111]",
    ],
)
def test_a_card_number_with_a_fraud_word_is_a_fraud_report(text: str) -> None:
    assert _reasons(_gate(message_masked=text, pan_found=True)) == ["fraud_report"]


def test_a_card_number_alone_or_fraud_words_alone_are_not_enough() -> None:
    plain = "Card [CARD ****1111] declined at the shop."
    assert _reasons(_gate(message_masked=plain, pan_found=True)) == []
    assert _reasons(_gate(message_masked="Is this fraud?", pan_found=False)) == []


@pytest.mark.parametrize("text", ["Is this a scammer?", "This is not meant badly", "unstolen"])
def test_fraud_words_match_whole_words_only(text: str) -> None:
    assert not has_fraud_words(POLICY, text)


def test_a_high_injection_score_alone_hands_off() -> None:
    """Owner decision (OR, security G4): the score or the triage flag, each on its own."""
    assert _reasons(_gate(injection_score=0.5)) == ["injection"]
    assert _reasons(_gate(injection_score=0.49)) == []
    both = _gate(injection_score=0.9, triage=_triage(injection_suspected=True))
    assert hard_rules(POLICY, both) == [
        RuleMatch("injection_suspected", RouteReason.INJECTION),
        RuleMatch("injection_score_high", RouteReason.INJECTION),
    ]
    assert reasons(hard_rules(POLICY, both)) == ["injection"]


# --- row 1: the order ---------------------------------------------------------------------------


@pytest.mark.parametrize("intent", ["account_restricted", "card_lost_stolen", "other"])
def test_a_handoff_intent_as_the_secondary_intent_hands_off_too(intent: str) -> None:
    """Owner decision (PR #36 review): the safe side, both intents count."""
    triage = _triage(intent="fees_fx", secondary_intent=intent)
    matches = hard_rules(POLICY, _gate(triage=triage))
    assert matches == [RuleMatch(intent, RouteReason.POLICY_INTENT)]


def test_a_lost_card_as_the_secondary_intent_still_gets_the_freeze_step() -> None:
    triage = _triage(intent="fees_fx", secondary_intent="card_lost_stolen")
    matches = hard_rules(POLICY, _gate(triage=triage))
    assert safe_first_steps(POLICY, matches) == [FREEZE]
    assert is_urgent(POLICY, matches, triage.urgency, triage.intent)


def test_both_intents_matching_give_one_reason() -> None:
    triage = _triage(intent="other", secondary_intent="card_lost_stolen")
    assert _reasons(_gate(triage=triage)) == ["policy_intent"]


def test_the_policy_topic_comes_before_a_restricted_account() -> None:
    """P4-02 review, owner decision: T-0014 (account blocked, restricted customer) is labelled
    `policy_intent`, not `restricted_account`."""
    given = _gate(triage=_triage(intent="account_restricted"), customer_restricted=True)
    assert _reasons(given) == ["policy_intent", "restricted_account"]


def test_every_matching_rule_is_listed_in_row_1_order() -> None:
    triage = _triage(
        intent="other",
        wants_human=True,
        reports_unauthorised_activity=True,
        is_complaint_or_legal=True,
        injection_suspected=True,
        asks_for_action="refund",
    )
    given = _gate(
        triage=triage,
        english=False,
        customer_restricted=True,
        pan_found=True,
        message_masked="fraud on [CARD ****1111]",
        injection_score=1.0,
    )
    assert _reasons(given) == [
        "policy_intent",
        "fraud_report",
        "injection",
        "complaint",
        "customer_request",
        "action_request",
        "restricted_account",
        "language",
    ]
    assert [reason.value for reason in pol.ROW_1_ORDER] == _reasons(given)


def test_the_dev_tickets_labelled_with_a_policy_intent_get_it_first() -> None:
    """T-0014 and T-0017 (P4-02 review): their labelled intent hands off first."""
    tickets = {
        row["id"]: row
        for row in map(json.loads, (ROOT / "data/eval/tickets.dev.jsonl").read_text().splitlines())
    }
    for ticket_id in ("T-0014", "T-0017"):
        ticket = tickets[ticket_id]
        given = _gate(
            triage=_triage(intent=ticket["intent"], urgency="high"),
            message_masked=ticket["message"],
            customer_restricted=True,
        )
        assert _reasons(given)[0] == ticket["expected_reason"] == "policy_intent"


def test_only_the_dev_ticket_labelled_language_is_not_english() -> None:
    """The language rule (owner decision: unknown = not English) on the 20 dev tickets: the
    Spanish one is handed off for its language, every English one passes."""
    rows = [
        json.loads(line) for line in (ROOT / "data/eval/tickets.dev.jsonl").read_text().splitlines()
    ]
    assert len(rows) == 20
    not_english = [row["id"] for row in rows if language(row["message"]) is None]
    labelled = [row["id"] for row in rows if row["expected_reason"] == "language"]
    assert not_english == labelled == ["T-0004"]


# --- rows 6 and 7 -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "proposed", "memory"),
    [
        ({"intent": "login_security"}, False, False),
        ({"intent": "dispute_chargeback"}, False, False),
        ({"asks_for_action": "dispute"}, False, False),
        ({"urgency": "high"}, False, False),
        ({}, True, False),
        ({}, False, True),
    ],
)
def test_each_approval_only_rule_needs_review(
    changes: dict[str, Any], proposed: bool, memory: bool
) -> None:
    assert needs_review(
        POLICY, _triage(**changes), has_proposed_actions=proposed, memory_shows_open_case=memory
    )


def test_a_routine_draft_needs_no_review() -> None:
    assert not needs_review(POLICY, _triage(), has_proposed_actions=False)


def test_review_rules_name_every_approval_only_rule_that_matched_in_file_order() -> None:
    """P12: the console shows which row-6 rule sent the draft to approval."""
    assert review_rules(
        POLICY,
        intent="dispute_chargeback",
        asks_for_action="dispute",
        high_urgency=True,
        has_proposed_actions=True,
        memory_shows_open_case=True,
    ) == ["intent", "action", "high_urgency", "has_proposed_actions", "memory_shows_open_case"]
    assert review_rules(
        POLICY,
        intent="withdrawal_issue",
        asks_for_action=None,
        high_urgency=False,
        has_proposed_actions=False,
        memory_shows_open_case=True,
    ) == ["memory_shows_open_case"]
    assert not review_rules(
        POLICY,
        intent=None,
        asks_for_action="none",
        high_urgency=False,
        has_proposed_actions=False,
        memory_shows_open_case=False,
    )


def test_the_intent_setting_can_remove_intents_but_never_add_them() -> None:
    assert auto_reply_intents(POLICY, ["fees_fx", "limits"]) == {"fees_fx", "limits"}
    assert auto_reply_intents(POLICY, ["fees_fx", "card_lost_stolen", "other"]) == {"fees_fx"}
    assert auto_reply_intents(POLICY, None) == frozenset()
    assert auto_reply_intents(POLICY, "fees_fx") == frozenset()


def test_the_allow_list_never_holds_a_handoff_or_review_intent() -> None:
    allowed = set(POLICY.auto_reply_allowlist.intents)
    assert not allowed & set(POLICY.always_handoff.intents)
    assert not allowed & set(POLICY.approval_only.intents)


# --- the loader -------------------------------------------------------------------------------


def _write(tmp_path: Path, change: Any) -> Path:  # noqa: ANN401 - any YAML change
    raw = yaml.safe_load(POLICY_YAML.read_text(encoding="utf-8"))
    change(raw)
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda raw: raw["always_handoff"]["intents"].append("crypto"), "always_handoff"),
        (lambda raw: raw["always_handoff"]["flags"].append("angry"), "always_handoff"),
        (lambda raw: raw["always_handoff"]["conditions"].append("full_moon"), "always_handoff"),
        (lambda raw: raw["approval_only"]["conditions"].append("maybe"), "approval_only"),
        (lambda raw: raw["forbidden_promises"]["refund"].append("(unclosed"), "refund"),
        (lambda raw: raw["support_hours"].update(weekdays="20:00-08:00"), "opening hours"),
        (lambda raw: raw["support_hours"].update(saturday="9-16"), "opening hours"),
        (lambda raw: raw["support_hours"].update(timezone="Mars/Olympus"), "time zone"),
        (lambda raw: raw["safe_first_step"].update(wrong_key=["x"]), "safe_first_step"),
        (lambda raw: raw["handoff_notices"].update(made_up="x"), "handoff_notices"),
        (lambda raw: raw["handoff_notices"].pop("default"), "default"),
        (lambda raw: raw["injection"].update(high_score=0), "high_score"),
        (lambda raw: raw.update(extra=1), "extra"),
    ],
)
def test_the_loader_refuses_a_broken_policy(tmp_path: Path, change: Any, message: str) -> None:  # noqa: ANN401
    with pytest.raises(PolicyError, match=message):
        load_policy(_write(tmp_path, change))


def test_the_loader_refuses_a_missing_or_unparsable_file(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match="cannot read"):
        load_policy(tmp_path / "missing.yaml")
    broken = tmp_path / "broken.yaml"
    broken.write_text("always_handoff: [", encoding="utf-8")
    with pytest.raises(PolicyError, match="cannot read"):
        load_policy(broken)


def test_the_real_file_loads_with_the_documented_rules() -> None:
    assert isinstance(POLICY, Policy)
    assert POLICY.always_handoff.intents == ["account_restricted", "card_lost_stolen", "other"]
    assert POLICY.approval_only.intents == ["login_security", "dispute_chargeback"]
    assert POLICY.injection.high_score == 0.5
    assert POLICY.support_hours.timezone == "Europe/Bratislava"


# --- the handoff notice -----------------------------------------------------------------------


def test_safe_first_steps_come_from_every_matching_rule() -> None:
    """Owner decision (P6-02): intent `other` decides the reason, but the unauthorised activity
    flag still adds the freeze step."""
    matches = hard_rules(
        POLICY, _gate(triage=_triage(intent="other", reports_unauthorised_activity=True))
    )
    assert reasons(matches) == ["policy_intent", "fraud_report"]
    assert safe_first_steps(POLICY, matches) == [FREEZE, NEVER_ASK]


def test_a_lost_card_gets_the_freeze_step_once() -> None:
    matches = hard_rules(
        POLICY,
        _gate(triage=_triage(intent="card_lost_stolen", reports_unauthorised_activity=True)),
    )
    assert safe_first_steps(POLICY, matches) == [FREEZE, NEVER_ASK]


def test_a_card_number_with_fraud_words_gets_the_fraud_steps() -> None:
    matches = hard_rules(POLICY, _gate(message_masked="fraud on [CARD ****1111]", pan_found=True))
    assert safe_first_steps(POLICY, matches) == [FREEZE, NEVER_ASK]


def test_a_reason_without_a_safe_step_adds_none() -> None:
    matches = hard_rules(POLICY, _gate(triage=_triage(wants_human=True)))
    assert safe_first_steps(POLICY, matches) == []


@pytest.mark.parametrize(
    ("changes", "matches", "urgent"),
    [
        ({"urgency": "high"}, [], True),
        ({"intent": "card_lost_stolen"}, [], True),
        ({"intent": "account_restricted"}, [], True),
        ({}, [RuleMatch("reports_unauthorised_activity", RouteReason.FRAUD_REPORT)], True),
        ({}, [RuleMatch("customer_restricted", RouteReason.RESTRICTED_ACCOUNT)], True),
        ({"intent": "other"}, [RuleMatch("card_lost_stolen", RouteReason.POLICY_INTENT)], True),
        ({}, [RuleMatch("wants_human", RouteReason.CUSTOMER_REQUEST)], False),
        ({}, [], False),
    ],
)
def test_urgency_follows_the_world_file(
    changes: dict[str, Any], matches: list[RuleMatch], urgent: bool
) -> None:
    triage = _triage(**changes)
    assert is_urgent(POLICY, matches, triage.urgency, triage.intent) is urgent


@pytest.mark.parametrize(
    ("now", "urgent", "expected"),
    [
        # Wednesday 12:00 CET: open
        (WEDNESDAY_NOON, True, "A support specialist will reply here within 30 minutes."),
        (WEDNESDAY_NOON, False, "A support specialist will reply here within 4 business hours."),
        # Wednesday 21:00 CET: closed, urgent cases go to the on-call analyst
        (
            datetime(2026, 11, 18, 20, 0, tzinfo=UTC),
            True,
            "A support specialist will reply here within 60 minutes.",
        ),
        (
            datetime(2026, 11, 18, 20, 0, tzinfo=UTC),
            False,
            "Our support team is back tomorrow at 08:00 CET. A support specialist will reply here"
            " within 4 business hours after that.",
        ),
        # Thursday 06:00 CET: before opening
        (
            datetime(2026, 11, 19, 5, 0, tzinfo=UTC),
            False,
            "Our support team is back today at 08:00 CET. A support specialist will reply here"
            " within 4 business hours after that.",
        ),
        # Friday 20:00 CET exactly: closed; Saturday opens at 10:00
        (
            datetime(2026, 11, 20, 19, 0, tzinfo=UTC),
            False,
            "Our support team is back tomorrow at 10:00 CET. A support specialist will reply here"
            " within 4 business hours after that.",
        ),
        # Saturday 17:00 CET: closed until Monday
        (
            datetime(2026, 11, 21, 16, 0, tzinfo=UTC),
            False,
            "Our support team is back on Monday at 08:00 CET. A support specialist will reply here"
            " within 4 business hours after that.",
        ),
        # Sunday in July (CEST): closed all day
        (
            datetime(2026, 7, 12, 10, 0, tzinfo=UTC),
            False,
            "Our support team is back tomorrow at 08:00 CEST. A support specialist will reply"
            " here within 4 business hours after that.",
        ),
    ],
)
def test_the_response_time_follows_the_support_hours(
    now: datetime, urgent: bool, expected: str
) -> None:
    assert response_time(POLICY, urgent=urgent, now=now) == expected


def test_saturday_hours_and_the_next_opening() -> None:
    saturday_noon = datetime(2026, 11, 21, 11, 0, tzinfo=UTC)  # 12:00 CET
    assert is_open(POLICY, saturday_noon)
    assert not is_open(POLICY, datetime(2026, 11, 22, 11, 0, tzinfo=UTC))  # Sunday
    opening = next_opening(POLICY, saturday_noon)
    assert (opening.weekday(), opening.hour) == (0, 8)


def test_the_notice_is_the_first_reasons_opening_the_steps_and_the_time() -> None:
    matches = hard_rules(POLICY, _gate(triage=_triage(intent="card_lost_stolen")))
    notice = handoff_notice(POLICY, reasons(matches), matches, urgent=True, now=WEDNESDAY_NOON)
    assert notice == (
        "This needs a support specialist, so I have passed your message to our team. "
        f"{FREEZE} A support specialist will reply here within 30 minutes."
    )


def test_a_reason_without_its_own_text_gets_the_default_opening() -> None:
    notice = handoff_notice(POLICY, ["injection"], [], urgent=False, now=WEDNESDAY_NOON)
    assert notice.startswith("I have passed your message to a support specialist. ")
    assert handoff_notice(POLICY, [], [], urgent=False, now=WEDNESDAY_NOON).startswith(
        "I have passed your message to a support specialist. "
    )


def test_every_reason_that_can_hand_off_has_an_opening_without_internals() -> None:
    """No notice names a restriction, an injection or another internal reason."""
    handoff_reasons = [*pol.ROW_1_ORDER, "budget_exceeded", "system_error", "masking_failed"]
    for reason in [*handoff_reasons, "no_kb_answer"]:
        text = handoff_notice(POLICY, [str(reason)], [], urgent=False, now=WEDNESDAY_NOON)
        lowered = text.lower()
        assert not any(word in lowered for word in ("restrict", "injection", "review", "fraud"))
