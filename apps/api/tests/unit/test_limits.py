"""Body cap (before anything parses the request) and the sliding-window rate limiter."""

from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.types import Message

from hub.api.errors import install_error_handlers
from hub.api.limits import MAX_BODY_BYTES, BodySizeLimitMiddleware, RateLimiter, RateLimits


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _app(max_bytes: int = 100) -> tuple[FastAPI, list[bytes]]:
    seen: list[bytes] = []
    app = FastAPI()
    install_error_handlers(app)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_bytes)

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        body = await request.body()
        seen.append(body)
        return {"size": len(body)}

    @app.get("/get")
    async def get() -> dict[str, str]:
        return {"ok": "yes"}

    return app, seen


def test_body_under_the_cap_reaches_the_app_unchanged() -> None:
    app, seen = _app()
    response = TestClient(app).post("/echo", content=b"x" * 100)
    assert response.status_code == 200
    assert seen == [b"x" * 100]


def test_declared_length_over_the_cap_is_refused_before_the_app() -> None:
    app, seen = _app()
    response = TestClient(app).post("/echo", content=b"x" * 101)
    assert response.status_code == 413
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "content_too_large"
    assert seen == []


def test_chunked_body_over_the_cap_is_refused() -> None:
    app, seen = _app()

    def chunks() -> Any:  # noqa: ANN401 - generator of bytes
        for _ in range(5):
            yield b"x" * 30

    # A generator body is sent chunked, without Content-Length.
    response = TestClient(app).post("/echo", content=chunks())
    assert response.status_code == 413
    assert seen == []


def test_invalid_content_length_is_refused() -> None:
    app, seen = _app()
    response = TestClient(app).post("/echo", content=b"x", headers={"Content-Length": "abc"})
    assert response.status_code == 413
    assert seen == []


def test_get_requests_pass_untouched() -> None:
    app, _ = _app()
    assert TestClient(app).get("/get").json() == {"ok": "yes"}


async def test_disconnect_while_reading_ends_quietly() -> None:
    called = False

    async def inner(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
        nonlocal called
        called = True

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "headers": []}
    await BodySizeLimitMiddleware(inner)(scope, receive, send)
    assert not called
    assert sent == []


async def test_replay_hands_on_to_the_real_receive_after_the_body() -> None:
    received: list[Message] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
        received.append(await receive())
        received.append(await receive())

    incoming: Iterator[Message] = iter(
        [
            {"type": "http.request", "body": b"ab", "more_body": True},
            {"type": "http.request", "body": b"cd", "more_body": False},
            {"type": "http.disconnect"},
        ]
    )

    async def receive() -> Message:
        return next(incoming)

    async def send(message: Message) -> None:
        pass

    scope = {"type": "http", "method": "POST", "headers": []}
    await BodySizeLimitMiddleware(inner, max_bytes=10)(scope, receive, send)
    assert received[0] == {"type": "http.request", "body": b"abcd", "more_body": False}
    assert received[1] == {"type": "http.disconnect"}


def test_default_cap_fits_a_fully_escaped_4000_character_message() -> None:
    # 4,000 characters as \uXXXX escapes plus the JSON around them.
    # 4,000 emoji as JSON-escaped surrogate pairs (12 bytes each) fit (P10-07).
    assert MAX_BODY_BYTES > 4_000 * 12 + 100


def test_rate_limiter_allows_the_limit_then_blocks_until_the_window_moves() -> None:
    clock = Clock()
    limiter = RateLimiter(3, 60, clock)
    for _ in range(3):
        assert limiter.retry_after("a") is None
        limiter.hit("a")
    assert limiter.retry_after("a") == 60
    assert limiter.retry_after("b") is None  # keys are independent
    clock.now += 59.5
    assert limiter.retry_after("a") == 1
    clock.now += 0.5
    assert limiter.retry_after("a") is None


def test_rate_limiter_sweeps_empty_keys() -> None:
    clock = Clock()
    limiter = RateLimiter(1, 10, clock, max_keys=2)
    limiter.hit("a")
    limiter.hit("b")
    clock.now += 11
    limiter.hit("c")  # three keys > max_keys: a and b are expired and dropped
    assert set(limiter._hits) == {"c"}


def test_demo_limits_match_the_api_contract() -> None:
    limits = RateLimits()
    assert (limits.messages_per_session.limit, limits.messages_per_session.window) == (10, 60)
    assert (limits.messages_per_ip.limit, limits.messages_per_ip.window) == (60, 3600)
    assert (
        limits.access_code_failures_per_ip.limit,
        limits.access_code_failures_per_ip.window,
    ) == (
        5,
        900,
    )
