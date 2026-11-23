"""FastAPI dependencies shared by the routers: container, database session, customer session,
operator session, roles and CSRF."""

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Cookie, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from hub.api.errors import ProblemError
from hub.auth.customer_session import COOKIE_NAME
from hub.auth.operator_session import COOKIE_NAME as OPERATOR_COOKIE_NAME
from hub.auth.operator_session import CSRF_HEADER, OperatorSession
from hub.auth.roles import RoleRequirement
from hub.db.models import Customer, Operator
from hub.db.repositories import CustomerRepository, OperatorRepository
from hub.deps import Container


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


async def db_session(container: ContainerDep) -> AsyncIterator[AsyncSession]:
    """One session per request. Handlers commit explicitly; anything not committed is rolled
    back when the session closes."""
    if container.database is None:
        raise RuntimeError("the app container has no database")
    async with container.database.sessions() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(db_session)]


def _unauthorized() -> ProblemError:
    return ProblemError(401, "unauthorized", "A customer session is required.")


def session_customer_id(
    container: ContainerDep,
    hub_customer: Annotated[str | None, Cookie(alias=COOKIE_NAME)] = None,
) -> uuid.UUID:
    """The customer id of a valid signed cookie. Runs before the database session is opened,
    so a request without a session costs no connection."""
    signer = container.session_signer
    customer_id = None if hub_customer is None or signer is None else signer.verify(hub_customer)
    if customer_id is None:
        raise _unauthorized()
    return customer_id


async def current_customer(
    customer_id: Annotated[uuid.UUID, Depends(session_customer_id)],
    session: SessionDep,
) -> Customer:
    """The customer of the signed session cookie. The identity never comes from the request
    body (AGENTS.md, never-do 3). An erased (anonymised) customer has no session."""
    customer = await CustomerRepository(session).get(customer_id)
    if customer is None or customer.anonymised_at is not None:
        raise _unauthorized()
    return customer


CustomerDep = Annotated[Customer, Depends(current_customer)]


def client_ip(request: Request) -> str:
    # Behind Cloud Run or Caddy, uvicorn --proxy-headers puts the client from X-Forwarded-For here
    # (trusted only from FORWARDED_ALLOW_IPS; the production image sets both, P10-06/P10-07).
    return request.client.host if request.client else "unknown"


# --- operator session -----------------------------------------------------------------------

OperatorCookie = Annotated[str | None, Cookie(alias=OPERATOR_COOKIE_NAME)]

# Requests that read only. Every other method on a console or admin route needs the CSRF token.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _operator_unauthorized() -> ProblemError:
    return ProblemError(401, "unauthorized", "An operator session is required.")


def operator_session(
    container: ContainerDep, hub_operator: OperatorCookie = None
) -> OperatorSession:
    """The signed operator cookie, checked before the database session is opened."""
    signer = container.operator_session_signer
    found = None if hub_operator is None or signer is None else signer.verify(hub_operator)
    if found is None:
        raise _operator_unauthorized()
    return found


async def current_operator(
    signed: Annotated[OperatorSession, Depends(operator_session)], session: SessionDep
) -> Operator:
    """The operator of the cookie, read from the database on every request: a deactivated
    operator, or a cookie from before a logout or password change (older `session_version`),
    has no session. The role used for checks is the stored one, never one from the cookie."""
    operator = await OperatorRepository(session).get(signed.operator_id)
    if (
        operator is None
        or not operator.is_active
        or operator.session_version != signed.session_version
    ):
        raise _operator_unauthorized()
    return operator


OperatorDep = Annotated[Operator, Depends(current_operator)]


def check_csrf(
    request: Request,
    container: ContainerDep,
    hub_operator: OperatorCookie = None,
    csrf_token: Annotated[str | None, Header(alias=CSRF_HEADER)] = None,
) -> None:
    """A state-changing request must carry the token of its own cookie (api.md §1)."""
    if request.method in _SAFE_METHODS:
        return
    signer = container.operator_session_signer
    if hub_operator is None or signer is None or not signer.csrf_matches(hub_operator, csrf_token):
        raise ProblemError(403, "csrf_failed", "The CSRF token is missing or not valid.")


class RequireRole:
    """Dependency of every console and admin route: a valid operator session, the CSRF token on
    state-changing requests, and one of the allowed roles. Order of the answers: 401 without a
    session, then 403 `csrf_failed`, then 403 `forbidden`.

    A class, so a test can find the requirement of each route in its dependency tree.
    """

    def __init__(self, requirement: RoleRequirement) -> None:
        self.requirement = requirement

    async def __call__(
        self, operator: OperatorDep, _csrf: Annotated[None, Depends(check_csrf)]
    ) -> Operator:
        if not self.requirement.allows(operator.role):
            raise ProblemError(403, "forbidden", "Your role does not allow this.")
        return operator
