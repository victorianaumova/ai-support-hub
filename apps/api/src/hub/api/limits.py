"""Input limits: a cap on the request body and the demo rate limits (docs/api.md §1, security T9).

The body cap runs before anything parses the request. Masking (G1) is synchronous CPU work,
about 12 ms per KB of digit-heavy text, so a large body must be refused before it reaches the
mask; the 4,000-character limit of a message is checked by the request schema, which also runs
before the mask. The cap leaves room for a 4,000-character message whose every character is a
JSON-escaped surrogate pair (an emoji written as `\\ud83d\\ude00`, 12 bytes: about 48 KB) plus
the other fields, so such a message reaches the 4,000-character check (422) instead of a 413
(P10-07, from the P2-06 review). Masking costs at most 4,000 characters either way.

The rate limiter keeps its windows in process memory. With more than one instance each one
counts on its own, so the deployment runs **one** instance at most (Cloud Run `max-instances 1`,
operations §6; P10-07 decision): the limits of api.md §1 then hold exactly. Behind a proxy the
client IP comes from `X-Forwarded-For`, which uvicorn trusts only from `FORWARDED_ALLOW_IPS`.
"""

import time
from collections import deque
from collections.abc import Callable

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from hub.api.errors import problem_response

MAX_BODY_BYTES = 64 * 1024
MAX_MESSAGE_CHARS = 4_000

_METHODS_WITH_BODY = frozenset({"POST", "PUT", "PATCH"})


class BodySizeLimitMiddleware:
    """Refuse bodies over `max_bytes` with 413 before the app reads them.

    The body is read here up to the cap and then replayed to the app, so a chunked request
    without `Content-Length` cannot get past the cap either.
    """

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in _METHODS_WITH_BODY:
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > self.max_bytes):
            await self._too_large(scope, receive, send)
            return

        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_bytes:
                await self._too_large(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break

        body = b"".join(chunks)
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            # After the body, the app only waits for a disconnect.
            return await receive()

        await self.app(scope, replay, send)

    async def _too_large(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = problem_response(
            413, "content_too_large", f"The request body is larger than {self.max_bytes} bytes."
        )
        await response(scope, receive, send)


class RateLimiter:
    """Sliding-window limit: at most `limit` hits per key in any `window_seconds`."""

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ) -> None:
        self.limit = limit
        self.window = window_seconds
        self._clock = clock
        self._max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}

    def retry_after(self, key: str) -> int | None:
        """Seconds until the next hit is allowed; `None` when it is allowed now."""
        hits = self._prune(key, self._clock())
        if len(hits) < self.limit:
            return None
        # At least 1: `Retry-After` is a whole number of seconds and 0 would invite a retry loop.
        return max(1, int(hits[0] + self.window - self._clock() + 0.999))

    def hit(self, key: str) -> None:
        now = self._clock()
        self._prune(key, now).append(now)
        if len(self._hits) > self._max_keys:
            self._sweep(now)

    def _prune(self, key: str, now: float) -> deque[float]:
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        return hits

    def _sweep(self, now: float) -> None:
        # Many distinct IPs would otherwise grow the dict without bound.
        for key in list(self._hits):
            if not self._prune(key, now):
                del self._hits[key]


class RateLimits:
    """The demo limits of docs/api.md §1. One instance lives in the app container."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.messages_per_session = RateLimiter(10, 60, clock)
        self.messages_per_ip = RateLimiter(60, 60 * 60, clock)
        # Wrong demo access codes: the same limit as the operator login.
        self.access_code_failures_per_ip = RateLimiter(5, 15 * 60, clock)
        # Operator login: 5 failed tries per 15 minutes per IP. The lockout of one account after
        # 10 failures is kept in the database instead (`hub/auth/lockout.py`).
        self.login_failures_per_ip = RateLimiter(5, 15 * 60, clock)
