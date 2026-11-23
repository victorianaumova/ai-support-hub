"""`GET` / `PUT /admin/settings` [lead] (docs/api.md §3, P10-09): the runtime settings a lead
changes in the console.

`decide` reads the settings on every run (`hub.graph.nodes.decide`) and the budget guard before
every model call (`hub.llm.budget`), so a change counts for the next ticket: switching
auto-reply off is the kill switch.

A `PUT` changes only the fields it sends and needs a `reason`. Each changed key writes one
`settings.changed` audit record with the old and new value and the reason, in the same
transaction as the change; a field sent with its current value writes nothing. The intent
allow-list may only remove intents from `auto_reply_allowlist` in `config/policy.yaml`.

Auto-reply can be switched on only where the deployment allows it (`AUTO_REPLY_ALLOWED`,
ADR-0019): otherwise a `PUT` with `auto_reply_enabled: true` is refused as a whole with
`409 auto_reply_not_allowed`, and nothing is written. Switching it off is always allowed.
"""

import math
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from sqlalchemy import select

from hub.api.dependencies import ContainerDep, RequireRole, SessionDep
from hub.api.errors import ProblemError
from hub.audit import record
from hub.auth.roles import LEAD
from hub.db.models import Operator, Setting
from hub.db.repositories import SettingsRepository
from hub.deps import Container
from hub.domain.enums import ActorType, Intent, SettingKey
from hub.guardrails.policy import Policy, load_policy

router = APIRouter(prefix="/api/v1/admin/settings", tags=["admin"])

# A daily budget above this is a typo, not a decision (the project's whole budget is $5).
MAX_DAILY_BUDGET_USD = 100.0


class SettingsOut(BaseModel):
    """The stored values; a missing or broken row is shown as the safe value `decide` uses."""

    auto_reply_enabled: bool
    confidence_threshold: float | None
    daily_budget_usd: float | None
    intent_allowlist: list[str]
    # The intents the policy allows at all: the most the allow-list can hold.
    allowed_intents: list[str]


class SettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_reply_enabled: bool | None = None
    confidence_threshold: Annotated[float, Field(gt=0, le=1)] | None = None
    daily_budget_usd: Annotated[float, Field(ge=0, le=MAX_DAILY_BUDGET_USD)] | None = None
    intent_allowlist: list[Intent] | None = None
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)]

    @field_validator("confidence_threshold", "daily_budget_usd")
    @classmethod
    def _finite(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("must be a finite number")
        return value


def _policy(container: Container) -> Policy:
    if container.pipeline is not None:
        return container.pipeline.policy
    return load_policy(container.settings.policy_config)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _out(stored: dict[SettingKey, Any], policy: Policy) -> SettingsOut:
    allow = stored.get(SettingKey.INTENT_ALLOWLIST)
    return SettingsOut(
        auto_reply_enabled=stored.get(SettingKey.AUTO_REPLY_ENABLED) is True,
        confidence_threshold=_number(stored.get(SettingKey.CONFIDENCE_THRESHOLD)),
        daily_budget_usd=_number(stored.get(SettingKey.DAILY_BUDGET_USD)),
        intent_allowlist=[str(i) for i in allow] if isinstance(allow, list) else [],
        allowed_intents=list(policy.auto_reply_allowlist.intents),
    )


def _invalid(field: str, error_type: str, message: str) -> ProblemError:
    return ProblemError(
        422,
        "validation_error",
        "The request does not match the schema.",
        extensions={"errors": [{"loc": ["body", field], "type": error_type, "msg": message}]},
    )


def changes(body: SettingsIn, policy: Policy) -> dict[SettingKey, Any]:
    """The JSON value of every field the request sends."""
    found: dict[SettingKey, Any] = {}
    if body.auto_reply_enabled is not None:
        found[SettingKey.AUTO_REPLY_ENABLED] = body.auto_reply_enabled
    if body.confidence_threshold is not None:
        found[SettingKey.CONFIDENCE_THRESHOLD] = body.confidence_threshold
    if body.daily_budget_usd is not None:
        found[SettingKey.DAILY_BUDGET_USD] = body.daily_budget_usd
    if body.intent_allowlist is not None:
        allowed = policy.auto_reply_allowlist.intents
        extra = [str(i) for i in body.intent_allowlist if str(i) not in allowed]
        if extra:
            raise _invalid(
                "intent_allowlist",
                "intent_not_allowed",
                "only intents of the policy's auto-reply allow-list can be listed",
            )
        # Policy order, no duplicates: the stored list does not depend on the request's order.
        chosen = {str(i) for i in body.intent_allowlist}
        found[SettingKey.INTENT_ALLOWLIST] = [i for i in allowed if i in chosen]
    if not found:
        raise _invalid("body", "no_change", "send at least one setting")
    return found


@router.get("")
async def get_settings(
    _operator: Annotated[Operator, Depends(RequireRole(LEAD))],
    session: SessionDep,
    container: ContainerDep,
) -> SettingsOut:
    return _out(await SettingsRepository(session).get_all(), _policy(container))


@router.put("")
async def put_settings(
    body: SettingsIn,
    operator: Annotated[Operator, Depends(RequireRole(LEAD))],
    session: SessionDep,
    container: ContainerDep,
) -> SettingsOut:
    if body.auto_reply_enabled is True and not container.settings.auto_reply_allowed:
        raise ProblemError(
            409,
            "auto_reply_not_allowed",
            "Auto-reply cannot be switched on in this deployment (AUTO_REPLY_ALLOWED is off).",
        )
    policy = _policy(container)
    wanted = changes(body, policy)
    repository = SettingsRepository(session)
    # Row locks: two leads saving at once must each audit the value they really replaced.
    await session.execute(select(Setting).with_for_update())
    stored = await repository.get_all()
    for key, value in wanted.items():
        old = stored.get(key)
        if old == value and type(old) is type(value):
            continue
        await repository.set(key, value)
        await record(
            session,
            actor_type=ActorType.OPERATOR,
            actor_id=operator.id,
            action="settings.changed",
            entity_type="setting",
            entity_id=str(key),
            details={"key": str(key), "old": old, "new": value, "reason": body.reason},
        )
        stored[key] = value
    await session.commit()
    return _out(stored, policy)
