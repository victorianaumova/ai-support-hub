"""Security headers and CSP on every response (security §4, T13; P8-04)."""

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.types import Receive, Scope, Send

from hub.api.limits import MAX_BODY_BYTES
from hub.api.security_headers import (
    CONTENT_SECURITY_POLICY,
    DOCS_CONTENT_SECURITY_POLICY,
    SecurityHeadersMiddleware,
    headers_for,
)
from hub.config import Settings
from hub.deps import Container
from hub.main import create_app

from .conftest import MakeClient

COMMON = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "cross-origin-opener-policy": "same-origin",
    "strict-transport-security": "max-age=31536000; includeSubDomains",
}


def _directives(policy: str) -> dict[str, str]:
    return {
        part.split(" ", 1)[0]: part.split(" ", 1)[1] if " " in part else ""
        for part in (p.strip() for p in policy.split(";"))
    }


def test_the_strict_policy_allows_only_the_own_origin() -> None:
    directives = _directives(CONTENT_SECURITY_POLICY)
    assert directives["default-src"] == "'self'"
    assert directives["script-src"] == "'self'"
    assert directives["style-src"] == "'self'"
    assert directives["connect-src"] == "'self'"
    assert directives["img-src"] == "'self' data:"
    assert directives["object-src"] == "'none'"
    assert directives["base-uri"] == "'none'"
    assert directives["frame-ancestors"] == "'none'"
    assert directives["form-action"] == "'self'"
    # No inline script, no eval, no third-party origin anywhere in the strict policy.
    assert "unsafe" not in CONTENT_SECURITY_POLICY
    assert "http" not in CONTENT_SECURITY_POLICY


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("/health", 200),
        ("/api/v1/tickets", 401),  # an error answer gets them too
        ("/api/v1/does-not-exist", 404),
        ("/boom", 500),  # the last-resort 500 of the request middleware
    ],
)
def test_every_answer_carries_the_headers(make_client: MakeClient, path: str, status: int) -> None:
    response = make_client().get(path)
    assert response.status_code == status
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    for name, value in COMMON.items():
        assert response.headers[name] == value
    assert "camera=()" in response.headers["permissions-policy"]


def test_a_413_of_the_body_cap_carries_the_headers(make_client: MakeClient) -> None:
    response = make_client().post(
        "/api/v1/tickets",
        content=b"x" * (MAX_BODY_BYTES + 1),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert response.headers["cache-control"] == "no-store"


def test_api_answers_are_never_cached_and_other_paths_are_left_alone(
    make_client: MakeClient,
) -> None:
    client = make_client()
    assert client.get("/api/v1/tickets").headers["cache-control"] == "no-store"
    assert "cache-control" not in client.get("/health").headers


def test_a_header_the_route_set_itself_is_kept() -> None:
    app = FastAPI()

    @app.get("/api/v1/stream")
    async def stream() -> StreamingResponse:
        async def events() -> AsyncIterator[bytes]:
            yield b"data: 1\n\n"
            yield b"data: 2\n\n"

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Frame-Options": "SAMEORIGIN"},
        )

    app.add_middleware(SecurityHeadersMiddleware)
    response = TestClient(app).get("/api/v1/stream")
    # The stream body passes through untouched; the route's own values win.
    assert response.text == "data: 1\n\ndata: 2\n\n"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY


async def test_non_http_scopes_pass_through() -> None:
    seen: list[str] = []

    async def inner(scope: Scope, _receive: Receive, _send: Send) -> None:
        seen.append(scope["type"])

    await SecurityHeadersMiddleware(inner)({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
    assert seen == ["lifespan"]


def test_docs_pages_get_their_own_policy_outside_the_demo(make_client: MakeClient) -> None:
    client = make_client()
    for path in ("/docs", "/redoc"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["content-security-policy"] == DOCS_CONTENT_SECURITY_POLICY
    assert headers_for("/docs")[0][1] == DOCS_CONTENT_SECURITY_POLICY
    # Only the exact docs pages: anything else keeps the strict policy.
    assert headers_for("/docs/other")[0][1] == CONTENT_SECURITY_POLICY
    assert "'unsafe-eval'" not in DOCS_CONTENT_SECURITY_POLICY


def test_the_public_demo_serves_no_docs_pages() -> None:
    settings = Settings(_env_file=None, app_env="demo")
    client = TestClient(create_app(Container(settings=settings)))
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    # The schema stays for `make web-types`.
    assert client.get("/openapi.json").status_code == 200
