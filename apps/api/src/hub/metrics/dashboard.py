"""Dashboard numbers from Postgres (docs/api.md §5, operations §3; P10-09).

Every number is computed in SQL over one period of whole UTC days, so a test can check it with
its own query. The ticket set of a period is the tickets **created** in it; route, route reasons,
drafts, latency, first response and feedback belong to those tickets. Cost is the real-mode
model spend recorded in the period (online and memory calls, the rows the budget guard counts,
without `record`). The budget box is always today (UTC), whatever the period.

A ticket's route is the route of its **first** run that has one: how the AI routed the ticket
when it arrived. A later run of the same ticket (a follow-up message) does not count again.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# The longest period one request may ask for (a year and a leap day).
MAX_DAYS = 366

TimeseriesMetric = Literal["tickets", "routes", "latency", "cost"]


@dataclass(frozen=True)
class Period:
    """Whole UTC days, both ends included."""

    start: date
    end: date

    @property
    def lower(self) -> datetime:
        return datetime.combine(self.start, time(), tzinfo=UTC)

    @property
    def upper(self) -> datetime:
        """Exclusive: 00:00 UTC of the day after `end`."""
        return datetime.combine(self.end + timedelta(days=1), time(), tzinfo=UTC)

    def days(self) -> list[date]:
        return [self.start + timedelta(days=n) for n in range((self.end - self.start).days + 1)]


class PeriodError(ValueError):
    """`from` after `to`, or a period longer than `MAX_DAYS`."""


def period(start: date | None, end: date | None, today: date) -> Period:
    """Default: the 7 days up to and including today. One missing end is filled from the other."""
    end = end or (start + timedelta(days=6) if start else today)
    start = start or end - timedelta(days=6)
    if start > end:
        raise PeriodError("from is after to")
    if (end - start).days + 1 > MAX_DAYS:
        raise PeriodError(f"a period has at most {MAX_DAYS} days")
    return Period(start, end)


class TicketCounts(BaseModel):
    total: int
    auto_reply: int
    approval: int
    handoff: int


class ReasonCount(BaseModel):
    reason: str
    count: int


class DraftCounts(BaseModel):
    approved: int
    edited: int
    rejected: int
    approved_unchanged_share: float | None
    median_edit_ratio: float | None


class Percentiles(BaseModel):
    p50: int | None
    p95: int | None


class CostOut(BaseModel):
    total: float
    per_ticket: float | None
    by_node: dict[str, float]


class FeedbackCounts(BaseModel):
    up: int
    down: int


class BudgetOut(BaseModel):
    daily_limit_usd: float
    spent_today_usd: float


class Summary(BaseModel):
    period: dict[str, date]
    tickets: TicketCounts
    automation_share: float | None
    ai_resolved_share: float | None
    route_reasons: list[ReasonCount]
    drafts: DraftCounts
    first_response_ms: Percentiles
    latency_ms: Percentiles
    cost_usd: CostOut
    feedback: FeedbackCounts
    budget: BudgetOut


class Point(BaseModel):
    date: date
    values: dict[str, float | int | None]


class Timeseries(BaseModel):
    metric: TimeseriesMetric
    bucket: Literal["day"]
    period: dict[str, date]
    points: list[Point]


# Every query starts from the tickets of the period and the route and reasons of their first
# routed run (the same CTE, written out in each query so every query is one fixed literal).

_TICKETS = """
WITH period_tickets AS (
    SELECT id, status, created_at
    FROM tickets
    WHERE created_at >= :lower AND created_at < :upper
),
first_routes AS (
    SELECT DISTINCT ON (r.ticket_id) r.ticket_id, r.route, r.route_reasons
    FROM runs r JOIN period_tickets t ON t.id = r.ticket_id
    WHERE r.route IS NOT NULL
    ORDER BY r.ticket_id, r.started_at, r.id
)
SELECT
    (SELECT count(*) FROM period_tickets),
    count(*) FILTER (WHERE route = 'auto_reply'),
    count(*) FILTER (WHERE route = 'approval'),
    count(*) FILTER (WHERE route = 'handoff')
FROM first_routes
"""

# Auto-replied, closed, no operator message, never asked for a human, no "not helpful" (PRD §6).
_AI_RESOLVED = """
WITH period_tickets AS (
    SELECT id, status, created_at
    FROM tickets
    WHERE created_at >= :lower AND created_at < :upper
),
first_routes AS (
    SELECT DISTINCT ON (r.ticket_id) r.ticket_id, r.route, r.route_reasons
    FROM runs r JOIN period_tickets t ON t.id = r.ticket_id
    WHERE r.route IS NOT NULL
    ORDER BY r.ticket_id, r.started_at, r.id
)
SELECT count(*)
FROM first_routes f JOIN period_tickets t ON t.id = f.ticket_id
WHERE f.route = 'auto_reply'
  AND t.status = 'closed'
  AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.ticket_id = t.id AND m.role = 'operator')
  AND NOT EXISTS (
      SELECT 1 FROM runs r
      WHERE r.ticket_id = t.id AND r.route_reasons ? 'customer_request'
  )
  AND NOT EXISTS (SELECT 1 FROM feedback fb WHERE fb.ticket_id = t.id AND fb.rating = 'down')
"""

_REASONS = """
WITH period_tickets AS (
    SELECT id, status, created_at
    FROM tickets
    WHERE created_at >= :lower AND created_at < :upper
),
first_routes AS (
    SELECT DISTINCT ON (r.ticket_id) r.ticket_id, r.route, r.route_reasons
    FROM runs r JOIN period_tickets t ON t.id = r.ticket_id
    WHERE r.route IS NOT NULL
    ORDER BY r.ticket_id, r.started_at, r.id
)
SELECT reason, count(*) AS n
FROM first_routes, jsonb_array_elements_text(route_reasons) AS reason
GROUP BY reason
ORDER BY n DESC, reason
"""

_DRAFTS = """
SELECT
    count(*) FILTER (WHERE d.status = 'approved'),
    count(*) FILTER (WHERE d.status = 'edited'),
    count(*) FILTER (WHERE d.status = 'rejected'),
    percentile_cont(0.5) WITHIN GROUP (ORDER BY d.edit_ratio) FILTER (WHERE d.status = 'edited')
FROM drafts d JOIN tickets t ON t.id = d.ticket_id
WHERE t.created_at >= :lower AND t.created_at < :upper
"""

_FIRST_RESPONSE = """
SELECT percentile_cont(0.5) WITHIN GROUP (
           ORDER BY extract(epoch FROM first_response_at - created_at) * 1000),
       percentile_cont(0.95) WITHIN GROUP (
           ORDER BY extract(epoch FROM first_response_at - created_at) * 1000)
FROM tickets
WHERE created_at >= :lower AND created_at < :upper AND first_response_at IS NOT NULL
"""

_LATENCY = """
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY r.latency_ms),
       percentile_cont(0.95) WITHIN GROUP (ORDER BY r.latency_ms)
FROM runs r JOIN tickets t ON t.id = r.ticket_id
WHERE t.created_at >= :lower AND t.created_at < :upper AND r.latency_ms IS NOT NULL
"""

# The calls the budget guard counts, without `record` (refreshing test recordings is not service).
_COST_BY_NODE = """
SELECT node, sum(cost_usd)
FROM llm_calls
WHERE mode = 'real' AND context IN ('online', 'memory')
  AND created_at >= :lower AND created_at < :upper
GROUP BY node ORDER BY node
"""

_FEEDBACK = """
SELECT count(*) FILTER (WHERE f.rating = 'up'), count(*) FILTER (WHERE f.rating = 'down')
FROM feedback f JOIN tickets t ON t.id = f.ticket_id
WHERE t.created_at >= :lower AND t.created_at < :upper
"""

_SERIES: dict[TimeseriesMetric, str] = {
    "tickets": """
SELECT ((created_at) AT TIME ZONE 'UTC')::date AS day, count(*) AS total
FROM tickets WHERE created_at >= :lower AND created_at < :upper
GROUP BY day
""",
    "routes": """
WITH period_tickets AS (
    SELECT id, status, created_at
    FROM tickets
    WHERE created_at >= :lower AND created_at < :upper
),
first_routes AS (
    SELECT DISTINCT ON (r.ticket_id) r.ticket_id, r.route, r.route_reasons
    FROM runs r JOIN period_tickets t ON t.id = r.ticket_id
    WHERE r.route IS NOT NULL
    ORDER BY r.ticket_id, r.started_at, r.id
)
SELECT ((t.created_at) AT TIME ZONE 'UTC')::date AS day,
       count(*) FILTER (WHERE f.route = 'auto_reply') AS auto_reply,
       count(*) FILTER (WHERE f.route = 'approval') AS approval,
       count(*) FILTER (WHERE f.route = 'handoff') AS handoff
FROM first_routes f JOIN period_tickets t ON t.id = f.ticket_id
GROUP BY day
""",
    "latency": """
SELECT ((t.created_at) AT TIME ZONE 'UTC')::date AS day,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY r.latency_ms) AS p50,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY r.latency_ms) AS p95
FROM runs r JOIN tickets t ON t.id = r.ticket_id
WHERE t.created_at >= :lower AND t.created_at < :upper AND r.latency_ms IS NOT NULL
GROUP BY day
""",
    "cost": """
SELECT ((created_at) AT TIME ZONE 'UTC')::date AS day, sum(cost_usd) AS total_usd
FROM llm_calls
WHERE mode = 'real' AND context IN ('online', 'memory')
  AND created_at >= :lower AND created_at < :upper
GROUP BY day
""",
}

_SERIES_EMPTY: dict[TimeseriesMetric, dict[str, float | int | None]] = {
    "tickets": {"total": 0},
    "routes": {"auto_reply": 0, "approval": 0, "handoff": 0},
    "latency": {"p50": None, "p95": None},
    "cost": {"total_usd": 0.0},
}


def share(part: int, whole: int) -> float | None:
    """`None` when there is nothing to divide: a dashboard shows "no data", not 0%."""
    return None if whole == 0 else round(part / whole, 4)


def _ms(value: Any) -> int | None:  # noqa: ANN401 - a numeric SQL result or NULL
    return None if value is None else round(float(value))


def _usd(value: Any) -> float:  # noqa: ANN401 - a numeric SQL result or NULL
    return round(float(value or 0), 6)


def _json_value(value: Any) -> float | int | None:  # noqa: ANN401 - a numeric SQL result
    if value is None or isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return round(float(value), 6)
    return round(float(value))


async def summary(
    session: AsyncSession, chosen: Period, *, daily_limit: Decimal, spent_today: Decimal
) -> Summary:
    params = {"lower": chosen.lower, "upper": chosen.upper}

    async def one(query: str) -> Any:  # noqa: ANN401 - one SQL row
        return (await session.execute(text(query), params)).one()

    total, auto_reply, approval, handoff = await one(_TICKETS)
    (ai_resolved,) = await one(_AI_RESOLVED)
    reasons = (await session.execute(text(_REASONS), params)).all()
    approved, edited, rejected, median_edit = await one(_DRAFTS)
    first_p50, first_p95 = await one(_FIRST_RESPONSE)
    latency_p50, latency_p95 = await one(_LATENCY)
    costs = await session.execute(text(_COST_BY_NODE), params)
    by_node = {node: _usd(cost) for node, cost in costs}
    up, down = await one(_FEEDBACK)
    cost_total = round(sum(by_node.values()), 6)
    reviewed = approved + edited + rejected
    return Summary(
        period={"from": chosen.start, "to": chosen.end},
        tickets=TicketCounts(
            total=total, auto_reply=auto_reply, approval=approval, handoff=handoff
        ),
        automation_share=share(auto_reply, total),
        ai_resolved_share=share(ai_resolved, total),
        route_reasons=[ReasonCount(reason=reason, count=n) for reason, n in reasons],
        drafts=DraftCounts(
            approved=approved,
            edited=edited,
            rejected=rejected,
            approved_unchanged_share=share(approved, reviewed),
            median_edit_ratio=None if median_edit is None else round(float(median_edit), 3),
        ),
        first_response_ms=Percentiles(p50=_ms(first_p50), p95=_ms(first_p95)),
        latency_ms=Percentiles(p50=_ms(latency_p50), p95=_ms(latency_p95)),
        cost_usd=CostOut(
            total=cost_total,
            per_ticket=None if total == 0 else round(cost_total / total, 6),
            by_node=by_node,
        ),
        feedback=FeedbackCounts(up=up, down=down),
        budget=BudgetOut(daily_limit_usd=float(daily_limit), spent_today_usd=_usd(spent_today)),
    )


async def timeseries(session: AsyncSession, metric: TimeseriesMetric, chosen: Period) -> Timeseries:
    """One point per day of the period, days without data filled with zeros (or `None` for a
    percentile, which has no value without data)."""
    params = {"lower": chosen.lower, "upper": chosen.upper}
    rows = (await session.execute(text(_SERIES[metric]), params)).mappings().all()
    found = {
        row["day"]: {key: _json_value(value) for key, value in row.items() if key != "day"}
        for row in rows
    }
    return Timeseries(
        metric=metric,
        bucket="day",
        period={"from": chosen.start, "to": chosen.end},
        points=[
            Point(date=day, values=found.get(day, dict(_SERIES_EMPTY[metric])))
            for day in chosen.days()
        ],
    )
