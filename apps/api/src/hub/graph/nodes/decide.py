"""decide node (docs/ai-pipeline.md §3, §6): code, after `verify`.

Rows 2 to 9 of the decision table (`hub.graph.routing.decide`, P6-02), with the rule version of
confidence and the console settings `auto_reply_enabled`, `confidence_threshold` and
`intent_allowlist` (read per run, so a change counts at once). A missing or broken setting is
the safe side: auto-reply off, nothing allowed, a threshold no confidence reaches. While the
deployment does not allow auto-reply (`AUTO_REPLY_ALLOWED`, ADR-0019) the stored
`auto_reply_enabled` is ignored and every draft goes to an operator.

`memory_shows_open_case` (P6-05): the customer has another open ticket with the triage intent,
or a memory item of the last 14 days on it (`hub.domain.memory.shows_open_case`).

A failed G1 mask of any model-written string of the run (triage summary, draft, missing
information, proposed actions, unsupported claims) hands the ticket off (`masking_failed`).
"""

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from hub.db.repositories import SettingsRepository
from hub.domain.enums import SettingKey
from hub.domain.memory import shows_open_case
from hub.graph.context import GraphContext
from hub.graph.draft import OutputMask
from hub.graph.nodes.intake import intake_result
from hub.graph.pipeline import Pipeline
from hub.graph.routing import DecideInput, decide
from hub.graph.state import TicketState
from hub.guardrails.policy import auto_reply_intents

# A threshold no confidence (at most 1) reaches: used when the setting is missing or broken.
NEVER = 2.0


async def read_settings(pipeline: Pipeline) -> dict[SettingKey, Any]:
    async with pipeline.database.sessions() as session:
        return await SettingsRepository(session).get_all()


def model_texts(state: TicketState) -> Iterable[str]:
    """Every model-written string of the run that is stored or sent."""
    triage = state.get("triage")
    if triage is not None:
        yield triage.summary
    draft = state.get("draft")
    if draft is not None:
        yield draft.answer
        if draft.missing_information:
            yield draft.missing_information
        for action in draft.proposed_actions:
            yield action.summary
            yield action.description
    verification = state.get("verification")
    if verification is not None:
        yield from (claim.claim for claim in verification.unsupported)


def mask_failed(state: TicketState) -> bool:
    clean = OutputMask(intake_result(state))
    for text in model_texts(state):
        clean(text)
    return clean.counts["failed"] > 0


def threshold(value: object) -> float:
    """The `confidence_threshold` setting; anything but a number in (0, 1] never allows an
    auto-reply (a threshold of 0 would let a confidence of 0 through, PR #36 review)."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 < value <= 1:
        return NEVER
    return float(value)


def auto_reply_on(pipeline: Pipeline, settings: dict[SettingKey, Any]) -> bool:
    """The stored setting counts only while the deployment allows auto-reply
    (`AUTO_REPLY_ALLOWED`, ADR-0019): a row switched on by hand or by an old version of the
    settings endpoint still sends the draft to an operator (`auto_reply_off`)."""
    return pipeline.auto_reply_allowed and settings.get(SettingKey.AUTO_REPLY_ENABLED) is True


def open_case(state: TicketState, now: datetime) -> bool:
    triage = state.get("triage")
    return triage is not None and shows_open_case(state.get("customer_context"), triage.intent, now)


async def run(state: TicketState, ctx: GraphContext) -> TicketState:
    pipeline = ctx.pipeline
    errors = state.get("errors", [])
    settings = await read_settings(pipeline) if not errors and state.get("draft") else {}
    decision = decide(
        pipeline.policy,
        pipeline.confidence_rule,
        DecideInput(
            errors=errors,
            triage=state.get("triage"),
            evidence=state.get("evidence"),
            draft=state.get("draft"),
            verification=state.get("verification"),
            retry_count=state.get("retry_count", 0),
            verifies=pipeline.verifies,
            mask_failed=mask_failed(state),
            auto_reply_enabled=auto_reply_on(pipeline, settings),
            allowed_intents=auto_reply_intents(
                pipeline.policy, settings.get(SettingKey.INTENT_ALLOWLIST)
            ),
            confidence_threshold=threshold(settings.get(SettingKey.CONFIDENCE_THRESHOLD, NEVER)),
            memory_shows_open_case=open_case(state, datetime.now(UTC)),
            variant=pipeline.variant,
        ),
    )
    return TicketState(
        route=decision.route,
        route_reasons=decision.reasons,
        confidence=decision.confidence,
        features=decision.features,
    )
