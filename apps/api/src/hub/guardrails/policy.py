"""The routing policy, `config/policy.yaml` (docs/ai-pipeline.md §6, ADR-0009 layer 1; P6-02).

Pure functions over the loaded file:
- `hard_rules`: row 1 of the decision table (`policy_gate`, before research). Every matching
  rule gives a `RuleMatch`; `reasons` lists their codes in the row-1 order of `RouteReason`
  (policy topic first, so `policy_intent` wins over `restricted_account`; owner decision, P4-02
  review). The first code decides; the safe first steps and the urgency of the handoff notice
  come from all of them (owner decision, P6-02).
- `needs_review`: row 6 (`approval_only`), with `review_rules` naming the rules that matched;
  `auto_reply_intents`: row 7's allow-list.
- `handoff_notice`: the fixed customer text of a handoff (ai-pipeline §3, FR-11): the opening of
  the first reason, the safe first steps, and the expected response time from `support_hours`.

A high G4 injection score and the triage flag `injection_suspected` each hand the ticket off on
their own (owner decision: OR, security G4). `customer_restricted` reads the profile's flag that
intake loads with `core_get_customer` (P7-04); it cannot match when no profile was loaded.
`memory_shows_open_case` uses memory (P6-05).

The loader refuses a file with an unknown intent, flag, action, condition or reason, an invalid
regular expression or invalid support hours, so a typo cannot switch a rule off silently.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from functools import cached_property
from pathlib import Path
from typing import Literal, get_args
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from hub.domain.enums import RouteReason
from hub.domain.triage import Intent, RequestedAction, TriageResult
from hub.guardrails.output_checks import promise_text

Flag = Literal[
    "wants_human", "reports_unauthorised_activity", "is_complaint_or_legal", "injection_suspected"
]
HandoffCondition = Literal[
    "language_not_english",
    "customer_restricted",
    "pan_in_message_with_fraud_words",
    "injection_score_high",
]
ReviewCondition = Literal["high_urgency", "has_proposed_actions", "memory_shows_open_case"]

INTENTS: frozenset[str] = frozenset(get_args(Intent))
FLAGS: frozenset[str] = frozenset(get_args(Flag))
ACTIONS: frozenset[str] = frozenset(get_args(RequestedAction))

# Row 1 reason of each flag and condition (ai-pipeline §6).
FLAG_REASONS: Mapping[str, RouteReason] = {
    "wants_human": RouteReason.CUSTOMER_REQUEST,
    "reports_unauthorised_activity": RouteReason.FRAUD_REPORT,
    "is_complaint_or_legal": RouteReason.COMPLAINT,
    "injection_suspected": RouteReason.INJECTION,
}
CONDITION_REASONS: Mapping[str, RouteReason] = {
    "language_not_english": RouteReason.LANGUAGE,
    "customer_restricted": RouteReason.RESTRICTED_ACCOUNT,
    "pan_in_message_with_fraud_words": RouteReason.FRAUD_REPORT,
    "injection_score_high": RouteReason.INJECTION,
}
# The row-1 order: the order of the codes in `RouteReason`.
ROW_1_ORDER: tuple[RouteReason, ...] = (
    RouteReason.POLICY_INTENT,
    RouteReason.FRAUD_REPORT,
    RouteReason.INJECTION,
    RouteReason.COMPLAINT,
    RouteReason.CUSTOMER_REQUEST,
    RouteReason.ACTION_REQUEST,
    RouteReason.RESTRICTED_ACCOUNT,
    RouteReason.LANGUAGE,
)
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_HOURS = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")


class PolicyError(Exception):
    """`config/policy.yaml` cannot be read or is invalid."""


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AlwaysHandoff(_Strict):
    intents: list[Intent]
    flags: list[Flag]
    actions: list[RequestedAction]
    conditions: list[HandoffCondition]


class ApprovalOnly(_Strict):
    intents: list[Intent]
    actions: list[RequestedAction]
    conditions: list[ReviewCondition]


class AutoReplyAllowlist(_Strict):
    intents: list[Intent]


class Injection(_Strict):
    high_score: float = Field(gt=0, le=1)


class Sla(_Strict):
    standard_business_hours: int = Field(gt=0)
    urgent_minutes: int = Field(gt=0)
    urgent_out_of_hours_minutes: int = Field(gt=0)


class SupportHours(_Strict):
    timezone: str
    weekdays: str
    saturday: str
    sla: Sla
    urgent_reasons: list[RouteReason]
    urgent_intents: list[Intent]

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone {value!r}") from exc
        return value

    @field_validator("weekdays", "saturday")
    @classmethod
    def _hours(cls, value: str) -> str:
        match = _HOURS.match(value)
        if match is None or match.group(1, 2) >= match.group(3, 4):
            raise ValueError(f"opening hours must be HH:MM-HH:MM, opening first: {value!r}")
        return value

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def hours_on(self, weekday: int) -> tuple[time, time] | None:
        """Opening and closing time on a weekday (0 = Monday); None when closed (Sunday)."""
        text = self.weekdays if weekday < 5 else self.saturday if weekday == 5 else None
        if text is None:
            return None
        start, end = text.split("-")
        return time.fromisoformat(start), time.fromisoformat(end)


class ResponseTime(_Strict):
    urgent: str
    standard: str
    standard_closed: str


class Policy(_Strict):
    always_handoff: AlwaysHandoff
    fraud_words: list[str] = Field(min_length=1)
    injection: Injection
    safe_first_step: dict[str, list[str]]
    approval_only: ApprovalOnly
    auto_reply_allowlist: AutoReplyAllowlist
    forbidden_promises: dict[str, list[str]]
    support_hours: SupportHours
    handoff_notices: dict[str, str]
    response_time: ResponseTime

    @field_validator("forbidden_promises")
    @classmethod
    def _compiles(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        for name, patterns in value.items():
            for pattern in patterns:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError(f"forbidden promise {name}: {exc}") from exc
        return value

    @model_validator(mode="after")
    def _known_keys(self) -> "Policy":
        reasons = {reason.value for reason in RouteReason}
        unknown_steps = set(self.safe_first_step) - INTENTS - FLAGS - reasons
        if unknown_steps:
            raise ValueError(f"safe_first_step: unknown keys {sorted(unknown_steps)}")
        unknown_notices = set(self.handoff_notices) - reasons - {"default"}
        if unknown_notices:
            raise ValueError(f"handoff_notices: unknown reasons {sorted(unknown_notices)}")
        if "default" not in self.handoff_notices:
            raise ValueError("handoff_notices: a `default` text is needed")
        return self

    @cached_property
    def fraud_pattern(self) -> re.Pattern[str]:
        words = sorted({promise_text(word) for word in self.fraud_words}, key=len, reverse=True)
        return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(w) for w in words) + r")(?!\w)")

    @cached_property
    def promises(self) -> Mapping[str, tuple[str, ...]]:
        """`forbidden_promises` in the shape the output checks take."""
        return {name: tuple(patterns) for name, patterns in self.forbidden_promises.items()}


def load_policy(path: Path) -> Policy:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PolicyError(f"cannot read {path}: {type(exc).__name__}") from exc
    try:
        return Policy.model_validate(raw)
    except ValidationError as exc:
        raise PolicyError(f"{path} is invalid: {exc}") from exc


# --- row 1: hard rules ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuleMatch:
    """One matching hard rule: its name (an intent, flag, action or condition of
    `always_handoff`) and its reason code."""

    rule: str
    reason: RouteReason


@dataclass(frozen=True, slots=True)
class GateInput:
    """What row 1 looks at. `english`: intake's language check (None, unknown, counts as not
    English; owner decision, P6-02). `pan_found`: G1 masked a card number in the message.
    `customer_restricted`: the profile's flag (P7-04); None when no profile was loaded."""

    triage: TriageResult
    message_masked: str
    english: bool | None
    pan_found: bool
    injection_score: float
    customer_restricted: bool | None = None


def has_fraud_words(policy: Policy, text: str) -> bool:
    return policy.fraud_pattern.search(promise_text(text)) is not None


def _conditions(policy: Policy, given: GateInput) -> Iterable[str]:
    found = {
        "language_not_english": given.english is not True,
        "customer_restricted": given.customer_restricted is True,
        "pan_in_message_with_fraud_words": given.pan_found
        and has_fraud_words(policy, given.message_masked),
        "injection_score_high": given.injection_score >= policy.injection.high_score,
    }
    return [name for name in policy.always_handoff.conditions if found[name]]


def hard_rules(policy: Policy, given: GateInput) -> list[RuleMatch]:
    """Every `always_handoff` rule that matches, in row-1 order of their reasons (a stable sort:
    within one reason, intents, then flags, actions and conditions in file order)."""
    rules = policy.always_handoff
    triage = given.triage
    matches: list[RuleMatch] = []
    # The secondary intent counts too (the safe side; owner decision, PR #36 review).
    for intent in (triage.intent, triage.secondary_intent):
        if intent is not None and intent in rules.intents:
            matches.append(RuleMatch(intent, RouteReason.POLICY_INTENT))
    matches += [
        RuleMatch(flag, FLAG_REASONS[flag]) for flag in rules.flags if getattr(triage, flag)
    ]
    if triage.asks_for_action in rules.actions:
        matches.append(RuleMatch(triage.asks_for_action, RouteReason.ACTION_REQUEST))
    matches += [RuleMatch(name, CONDITION_REASONS[name]) for name in _conditions(policy, given)]
    return sorted(matches, key=lambda match: ROW_1_ORDER.index(match.reason))


def reasons(matches: Sequence[RuleMatch]) -> list[str]:
    """The reason codes of the matches, without repeats, first one deciding."""
    return list(dict.fromkeys(match.reason.value for match in matches))


# --- rows 6 and 7 ---------------------------------------------------------------------------


def needs_review(
    policy: Policy,
    triage: TriageResult,
    *,
    has_proposed_actions: bool,
    memory_shows_open_case: bool = False,
) -> bool:
    """Row 6 (`approval_only`): the AI may draft, a human must send."""
    return bool(
        review_rules(
            policy,
            intent=triage.intent,
            asks_for_action=triage.asks_for_action,
            high_urgency=triage.urgency == "high",
            has_proposed_actions=has_proposed_actions,
            memory_shows_open_case=memory_shows_open_case,
        )
    )


ReviewRule = Literal[
    "intent", "action", "high_urgency", "has_proposed_actions", "memory_shows_open_case"
]


def review_rules(
    policy: Policy,
    *,
    intent: str | None,
    asks_for_action: str | None,
    high_urgency: bool,
    has_proposed_actions: bool,
    memory_shows_open_case: bool,
) -> list[ReviewRule]:
    """Which rules of row 6 (`approval_only`) match, in the order of the file: `intent`,
    `action`, then each condition. The console shows them under "Why you see this ticket"."""
    rules = policy.approval_only
    found: dict[ReviewCondition, bool] = {
        "high_urgency": high_urgency,
        "has_proposed_actions": has_proposed_actions,
        "memory_shows_open_case": memory_shows_open_case,
    }
    matched: list[ReviewRule] = []
    if intent is not None and intent in rules.intents:
        matched.append("intent")
    if asks_for_action is not None and asks_for_action in rules.actions:
        matched.append("action")
    matched += [name for name in rules.conditions if found[name]]
    return matched


def auto_reply_intents(policy: Policy, setting: object) -> frozenset[str]:
    """Row 7's allow-list: the `intent_allowlist` setting, limited to the policy's list (a
    setting can remove intents, never add them). A missing or broken setting allows nothing."""
    if not isinstance(setting, list):
        return frozenset()
    return frozenset(policy.auto_reply_allowlist.intents) & {str(item) for item in setting}


# --- the handoff notice ---------------------------------------------------------------------


def safe_first_steps(policy: Policy, matches: Sequence[RuleMatch]) -> list[str]:
    """The steps of every match, keyed by its rule (intent or flag) or its reason, without
    repeats, in the order of the matches."""
    steps: list[str] = []
    for match in matches:
        for key in (match.rule, match.reason.value):
            steps += policy.safe_first_step.get(key, [])
    return list(dict.fromkeys(steps))


def is_urgent(
    policy: Policy, matches: Sequence[RuleMatch], urgency: str | None, intent: str | None
) -> bool:
    """World file §8: fraud, a lost card or a restricted account, or triage urgency `high`."""
    hours = policy.support_hours
    return (
        urgency == "high"
        or intent in hours.urgent_intents
        or any(match.reason in hours.urgent_reasons for match in matches)
        or any(match.rule in hours.urgent_intents for match in matches)
    )


def is_open(policy: Policy, now: datetime) -> bool:
    local = now.astimezone(policy.support_hours.zone)
    hours = policy.support_hours.hours_on(local.weekday())
    return hours is not None and hours[0] <= local.time() < hours[1]


def next_opening(policy: Policy, now: datetime) -> datetime:
    """The next time support opens after `now` (aware datetime in the policy's zone)."""
    zone = policy.support_hours.zone
    local = now.astimezone(zone)
    for days in range(8):
        day = (local + timedelta(days=days)).date()
        hours = policy.support_hours.hours_on(day.weekday())
        if hours is None:
            continue
        opening = datetime.combine(day, hours[0], tzinfo=zone)
        if opening > local:
            return opening
    raise PolicyError("support is never open")  # pragma: no cover - the loader needs two days


def _opening_text(opening: datetime, now: datetime) -> str:
    local_now = now.astimezone(opening.tzinfo)
    if opening.date() == local_now.date():
        day = "today"
    elif opening.date() == local_now.date() + timedelta(days=1):
        day = "tomorrow"
    else:
        day = f"on {WEEKDAYS[opening.weekday()]}"
    return f"{day} at {opening:%H:%M} {opening.tzname()}"


def response_time(policy: Policy, *, urgent: bool, now: datetime) -> str:
    sla = policy.support_hours.sla
    texts = policy.response_time
    open_now = is_open(policy, now)
    if urgent:
        minutes = sla.urgent_minutes if open_now else sla.urgent_out_of_hours_minutes
        return texts.urgent.format(minutes=minutes)
    if open_now:
        return texts.standard.format(hours=sla.standard_business_hours)
    opens = _opening_text(next_opening(policy, now), now)
    return texts.standard_closed.format(opens=opens, hours=sla.standard_business_hours)


def handoff_notice(
    policy: Policy,
    route_reasons: Sequence[str],
    matches: Sequence[RuleMatch],
    *,
    urgent: bool,
    now: datetime,
) -> str:
    """The customer notice: the opening of the first reason, the safe first steps of all
    matches, the expected response time. Fixed texts only: no model output goes into it."""
    first = route_reasons[0] if route_reasons else "default"
    opening = policy.handoff_notices.get(first, policy.handoff_notices["default"])
    parts = [
        opening,
        *safe_first_steps(policy, matches),
        response_time(policy, urgent=urgent, now=now),
    ]
    return " ".join(parts)
