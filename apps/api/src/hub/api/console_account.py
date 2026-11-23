"""The console's account view (docs/api.md §3, UI §3 *Customer* tab; owner decision on PR #66).

`GET /console/tickets/{id}/account` reads the ticket customer's profile (limits used, restricted
flag), cards and newest transactions from the core mock now, through `hub.tools` like every
other tool call: allow-list `console` (the three read tools, no side effect), the ticket's latest
run as the scope (the calls are recorded there with their audit records), the customer reference
from the tickets row (never from the request), and the per-run limit of the node. The answer is
built field by field into the output models (only what the tab shows) and its texts are masked
(G1) again before they leave the API.

Operators only: compliance reads tickets but has no need for live account data. A part whose
call was refused or failed is null and named in `unavailable`; the console shows it as unknown.
"""

import logging
import uuid
from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter
from sqlalchemy import select

from hub.api.console import Writer, _ticket_or_404
from hub.api.console_schemas import AccountOut, AccountProfileOut, CardOut, TransactionOut
from hub.api.dependencies import ContainerDep, SessionDep
from hub.api.errors import ProblemError
from hub.db.models import Customer
from hub.db.repositories import RunRepository
from hub.domain.enums import ToolCallStatus
from hub.guardrails.masking import mask_text
from hub.tools.permissions import ToolNode, ToolScope

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/console", tags=["console"])

# The newest transactions the tab shows.
RECENT_TRANSACTIONS = 10

Part = Literal["profile", "cards", "transactions"]


def masked(value: Any) -> Any:  # noqa: ANN401 - any JSON value
    """Every string of a tool result masked (G1); numbers, flags and nulls as they are."""
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, list):
        return [masked(item) for item in value]
    if isinstance(value, dict):
        return {key: masked(item) for key, item in value.items()}
    return value


@router.get("/tickets/{ticket_id}/account")
async def get_account(
    ticket_id: uuid.UUID, _operator: Writer, session: SessionDep, container: ContainerDep
) -> AccountOut:
    pipeline = container.pipeline
    tools = None if pipeline is None else pipeline.tools
    if tools is None:
        raise ProblemError(503, "tools_unavailable", "Account data is not available.")
    ticket = await _ticket_or_404(session, ticket_id)
    run = await RunRepository(session).latest_for_ticket(ticket.id)
    if run is None:
        raise ProblemError(409, "conflict", "This ticket has no run to record the reads in.")
    customer_ref = await session.scalar(
        select(Customer.external_ref).where(Customer.id == ticket.customer_id)
    )
    if customer_ref is None:  # pragma: no cover - a ticket always has its customer
        raise ProblemError(404, "not_found", "Ticket not found.")
    # The calls are recorded in their own transactions; nothing here needs this one.
    await session.commit()
    scope = ToolScope(
        run_id=run.id, ticket_id=ticket.id, customer_ref=customer_ref, node=ToolNode.CONSOLE
    )
    calls = tools.session(scope)
    unavailable: list[Part] = []

    async def read[T](
        part: Part, name: str, arguments: dict[str, Any], build: Callable[[dict[str, Any]], T]
    ) -> T | None:
        try:
            outcome = await calls.call(name, arguments)
            if outcome.status is ToolCallStatus.OK and outcome.result is not None:
                return build(masked(outcome.result))
        except Exception:  # any failure leaves the part unknown; the call row says why
            logger.warning("account read failed", extra={"tool": name, "ticket_id": str(ticket_id)})
        unavailable.append(part)
        return None

    profile = await read(
        "profile",
        "core_get_customer",
        {},
        AccountProfileOut.model_validate,
    )
    cards = await read(
        "cards",
        "core_get_cards",
        {},
        lambda data: [CardOut.model_validate(card) for card in data["cards"]],
    )
    transactions = await read(
        "transactions",
        "core_list_transactions",
        {"limit": RECENT_TRANSACTIONS},
        lambda data: [TransactionOut.model_validate(tx) for tx in data["transactions"]],
    )
    return AccountOut(
        profile=profile, cards=cards, transactions=transactions, unavailable=unavailable
    )
