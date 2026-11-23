"""The demo customer login (docs/api.md §2). DEMO ONLY.

`GET /demo/customers` lists the demo customers for the chat's picker (P10-02), behind the access
code; `POST /demo/session` stands in for the login of the Tallybridge app (see
`hub/auth/customer_session.py`).

The picker list is `DEMO_CUSTOMERS_FILE` (`data/seed/demo_customers.json`): which customers to
show, a short hint and suggested first messages. Name and tier come from the database, so an
erased (anonymised) or missing customer is left out.
"""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import ValidationError

from hub.api.dependencies import ContainerDep, SessionDep, client_ip
from hub.api.errors import ProblemError
from hub.api.schemas import CustomerProfileOut, DemoCustomerOut, DemoSessionIn
from hub.audit import record
from hub.auth.customer_session import (
    COOKIE_NAME,
    MAX_AGE_SECONDS,
    CustomerSessionSigner,
    access_code_matches,
)
from hub.db.repositories import CustomerRepository
from hub.domain.enums import ActorType
from hub.domain.seed import DEMO_CUSTOMERS, DemoCustomerEntry

router = APIRouter(prefix="/api/v1/demo", tags=["demo"])


def load_demo_customers(path: Path) -> list[DemoCustomerEntry]:
    """The picker list; a missing or broken file is a configuration error (503), never a crash."""
    try:
        return DEMO_CUSTOMERS.validate_json(path.read_bytes())
    except (OSError, ValidationError) as error:
        raise ProblemError(503, "demo_unavailable", "The demo chat is not configured.") from error


def demo_signer(container: ContainerDep) -> CustomerSessionSigner:
    """503 while SESSION_SECRET or DEMO_ACCESS_CODE is empty: the demo chat is switched off.
    Resolved before the database session, so a switched-off demo opens no connection."""
    signer = container.session_signer
    if signer is None or not container.settings.demo_access_code.get_secret_value():
        raise ProblemError(503, "demo_unavailable", "The demo chat is not configured.")
    return signer


def _check_access_code(container: ContainerDep, request: Request, given: str) -> None:
    """The limit of wrong codes per IP is shared with `POST /demo/session`."""
    limiter = container.rate_limits.access_code_failures_per_ip
    ip = client_ip(request)
    retry_after = limiter.retry_after(ip)
    if retry_after is not None:
        raise ProblemError(
            429,
            "too_many_requests",
            "Too many wrong access codes. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )
    if not access_code_matches(given, container.settings.demo_access_code.get_secret_value()):
        limiter.hit(ip)
        raise ProblemError(401, "invalid_access_code", "The access code is not valid.")


@router.get("/customers")
async def list_demo_customers(
    request: Request,
    container: ContainerDep,
    _signer: Annotated[CustomerSessionSigner, Depends(demo_signer)],
    session: SessionDep,
    access_code: Annotated[str, Header(alias="X-Demo-Access-Code", min_length=1, max_length=200)],
) -> list[DemoCustomerOut]:
    _check_access_code(container, request, access_code)
    entries = load_demo_customers(container.settings.demo_customers_file)
    customers = CustomerRepository(session)
    found: list[DemoCustomerOut] = []
    for entry in entries:
        customer = await customers.get_by_external_ref(entry.customer_ref)
        if customer is None or customer.anonymised_at is not None:
            continue
        found.append(
            DemoCustomerOut(
                customer_ref=customer.external_ref,
                display_name=customer.display_name,
                tier=customer.tier,
                hint=entry.hint,
                suggestions=list(entry.suggestions),
            )
        )
    return found


@router.post("/session")
async def create_demo_session(
    body: DemoSessionIn,
    request: Request,
    response: Response,
    container: ContainerDep,
    signer: Annotated[CustomerSessionSigner, Depends(demo_signer)],
    session: SessionDep,
) -> CustomerProfileOut:
    _check_access_code(container, request, body.access_code)

    customer = await CustomerRepository(session).get_by_external_ref(body.customer_ref)
    if customer is None or customer.anonymised_at is not None:
        raise ProblemError(404, "not_found", "No demo customer with this reference.")

    await record(
        session,
        actor_type=ActorType.CUSTOMER,
        actor_id=customer.id,
        action="auth.demo_session",
        entity_type="customer",
        entity_id=customer.id,
    )
    await session.commit()

    response.set_cookie(
        COOKIE_NAME,
        signer.sign(customer.id),
        max_age=MAX_AGE_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return CustomerProfileOut(
        customer_ref=customer.external_ref,
        display_name=customer.display_name,
        tier=customer.tier,
    )
