"""The Cloud Run ID token for the MCP service (P10-07): fetched from the metadata server, cached,
sent in `X-Serverless-Authorization` next to the MCP bearer token."""

import httpx2
import pytest

from hub.observability.readiness import mcp_check
from hub.tools.cloud_identity import IdentityToken, IdentityTokenError


def metadata(tokens: list[str], seen: list[httpx2.Request]) -> httpx2.MockTransport:
    def answer(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, text=tokens[len(seen) - 1])

    return httpx2.MockTransport(answer)


async def test_the_token_is_fetched_for_the_audience_and_cached_until_near_expiry() -> None:
    seen: list[httpx2.Request] = []
    now = [0.0]
    identity = IdentityToken("https://tools.run.app", metadata(["t1", "t2"], seen), lambda: now[0])

    assert await identity.headers() == {"X-Serverless-Authorization": "Bearer t1"}
    now[0] = 3000
    assert await identity.token() == "t1"
    now[0] = 3301  # inside the five-minute margin before the hour ends
    assert await identity.token() == "t2"

    assert len(seen) == 2
    assert seen[0].headers["Metadata-Flavor"] == "Google"
    assert seen[0].url.params["audience"] == "https://tools.run.app"


async def test_no_token_from_the_metadata_server_is_an_error() -> None:
    transport = httpx2.MockTransport(lambda request: httpx2.Response(404))
    with pytest.raises(IdentityTokenError):
        await IdentityToken("aud", transport).token()


async def test_the_readiness_check_sends_both_tokens() -> None:
    seen: list[httpx2.Request] = []

    def server(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(406)

    async def extra() -> dict[str, str]:
        return {"X-Serverless-Authorization": "Bearer id-token"}

    check = mcp_check("https://tools.run.app/mcp", "mcp-token", httpx2.MockTransport(server), extra)
    await check()

    assert seen[0].headers["Authorization"] == "Bearer mcp-token"
    assert seen[0].headers["X-Serverless-Authorization"] == "Bearer id-token"
