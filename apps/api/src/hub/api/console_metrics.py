"""Lead and compliance read endpoints (docs/api.md §3, §5; P10-09): dashboard metrics, the newest
evaluation summary, the audit log and its chain check.

All are `GET`; none changes anything. The numbers are defined in `hub.metrics.dashboard`.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select

from hub.api.dependencies import ContainerDep, RequireRole, SessionDep
from hub.api.errors import ProblemError
from hub.api.schemas import Page
from hub.audit import ChainCheck, verify_chain
from hub.auth.roles import AUDIT_READER, LEAD
from hub.db.models import AuditLog, Operator
from hub.llm.budget import BudgetGuard
from hub.metrics import dashboard, evals
from hub.metrics.dashboard import Period, PeriodError, Summary, Timeseries, TimeseriesMetric
from hub.metrics.evals import EvalSummary

router = APIRouter(prefix="/api/v1/console", tags=["console"])

FromQuery = Annotated[date | None, Query(alias="from")]
ToQuery = Annotated[date | None, Query(alias="to")]


def _period(start: date | None, end: date | None) -> Period:
    try:
        return dashboard.period(start, end, datetime.now(UTC).date())
    except PeriodError as error:
        raise ProblemError(
            422,
            "validation_error",
            "The request does not match the schema.",
            extensions={
                "errors": [{"loc": ["query", "from"], "type": "period", "msg": str(error)}]
            },
        ) from error


@router.get("/metrics/summary")
async def metrics_summary(
    _operator: Annotated[Operator, Depends(RequireRole(LEAD))],
    session: SessionDep,
    container: ContainerDep,
    start: FromQuery = None,
    end: ToQuery = None,
) -> Summary:
    if container.database is None:
        raise RuntimeError("the app container has no database")
    # The same numbers the budget guard of the gateway uses (same table, same setting).
    guard = BudgetGuard(
        container.database, default_limit=Decimal(str(container.settings.daily_budget_usd))
    )
    return await dashboard.summary(
        session,
        _period(start, end),
        daily_limit=await guard.limit(),
        spent_today=await guard.spent_today(),
    )


@router.get("/metrics/timeseries")
async def metrics_timeseries(
    _operator: Annotated[Operator, Depends(RequireRole(LEAD))],
    session: SessionDep,
    metric: TimeseriesMetric,
    bucket: Annotated[str, Query(pattern="^day$")] = "day",
    start: FromQuery = None,
    end: ToQuery = None,
) -> Timeseries:
    del bucket  # only `day` exists; the parameter is in the contract for later buckets
    return await dashboard.timeseries(session, metric, _period(start, end))


class EvalLatestOut(BaseModel):
    """`run` is null when no clean real-model evaluation report is available."""

    run: EvalSummary | None


@router.get("/evals/latest")
async def evals_latest(
    _operator: Annotated[Operator, Depends(RequireRole(LEAD))], container: ContainerDep
) -> EvalLatestOut:
    return EvalLatestOut(run=evals.latest(container.settings.eval_runs_dir))


class AuditOut(BaseModel):
    id: int
    ts: datetime
    actor_type: str
    actor_id: str | None
    action: str
    entity_type: str
    entity_id: str
    details: dict[str, Any]


@router.get("/audit")
async def audit_log(
    _operator: Annotated[Operator, Depends(RequireRole(AUDIT_READER))],
    session: SessionDep,
    entity_type: Annotated[str | None, Query(max_length=100)] = None,
    entity_id: Annotated[str | None, Query(max_length=100)] = None,
    actor_id: Annotated[str | None, Query(max_length=100)] = None,
    action: Annotated[str | None, Query(max_length=100)] = None,
    start: Annotated[datetime | None, Query(alias="from")] = None,
    end: Annotated[datetime | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(pattern="^[0-9]{1,19}$")] = None,
) -> Page[AuditOut]:
    """Newest first. The cursor is the id of the last row of the previous page."""
    query = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit + 1)
    for column, value in (
        (AuditLog.entity_type, entity_type),
        (AuditLog.entity_id, entity_id),
        (AuditLog.actor_id, actor_id),
        (AuditLog.action, action),
    ):
        if value is not None:
            query = query.where(column == value)
    if start is not None:
        query = query.where(AuditLog.ts >= start)
    if end is not None:
        query = query.where(AuditLog.ts < end)
    if cursor is not None:
        query = query.where(AuditLog.id < int(cursor))
    rows = list((await session.execute(query)).scalars())
    more = len(rows) > limit
    rows = rows[:limit]
    return Page[AuditOut](
        items=[
            AuditOut(
                id=row.id,
                ts=row.ts,
                actor_type=str(row.actor_type),
                actor_id=row.actor_id,
                action=row.action,
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                details=row.details,
            )
            for row in rows
        ],
        next_cursor=str(rows[-1].id) if more and rows else None,
    )


@router.get("/audit/verify")
async def audit_verify(
    _operator: Annotated[Operator, Depends(RequireRole(AUDIT_READER))], session: SessionDep
) -> ChainCheck:
    """`ok`, the number of rows checked, and the id of the first broken row."""
    return await verify_chain(session)
