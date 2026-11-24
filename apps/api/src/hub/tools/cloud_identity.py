"""Google ID token for calling the MCP server on Cloud Run (operations §6, P10-07).

The `support-tools` service requires authentication: only the API's service account has the
invoker role. Cloud Run checks a Google ID token; the MCP server's own bearer token
(`MCP_SERVER_TOKEN`) stays in `Authorization` as the second check, so the ID token goes in
`X-Serverless-Authorization`, which Cloud Run reads and removes before the request reaches the
container (Cloud Run docs, "Authenticating service-to-service").

The token comes from the metadata server of the running service, for the audience
`MCP_SERVER_AUDIENCE` (the MCP service URL). It is cached until five minutes before it expires
(tokens live one hour). Empty audience = no ID token (local stack, tests).
"""

import asyncio
import time
from collections.abc import Callable

import httpx2

METADATA_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity"
)
# Google ID tokens are valid for one hour; refresh well before.
TOKEN_LIFETIME_S = 3600
REFRESH_MARGIN_S = 300


class IdentityTokenError(Exception):
    """The metadata server gave no token (not on Google Cloud, or no service account)."""


class IdentityToken:
    def __init__(
        self,
        audience: str,
        transport: httpx2.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._audience = audience
        self._transport = transport
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def token(self) -> str:
        async with self._lock:
            if self._token is None or self._clock() >= self._expires_at - REFRESH_MARGIN_S:
                self._token = await self._fetch()
                self._expires_at = self._clock() + TOKEN_LIFETIME_S
            return self._token

    async def headers(self) -> dict[str, str]:
        return {"X-Serverless-Authorization": f"Bearer {await self.token()}"}

    async def _fetch(self) -> str:
        async with httpx2.AsyncClient(transport=self._transport, timeout=5) as client:
            response = await client.get(
                METADATA_URL,
                params={"audience": self._audience},
                headers={"Metadata-Flavor": "Google"},
            )
        if response.status_code != 200 or not response.text:
            raise IdentityTokenError(f"metadata server answered HTTP {response.status_code}")
        return response.text.strip()
