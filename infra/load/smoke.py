"""Load smoke (P10-08): 10 parallel customers send 50 tickets through the real API and the full
workflow, with FAKE models only (`LLM_MODE=fake`, recorded answers: no model cost, ADR-0018).

Each ticket is the SEPA question that has a recorded answer, so every run goes through triage,
search, write and verify. Latency = from the request until the stream's `done` event (the run
works inside the request, ADR-0007). Afterwards every ticket is read back: one still in
`processing` is a lost run. Fake models answer in milliseconds, so **model time is not
included**; the numbers show the service around the models, not answer latency.

    uv run python infra/load/smoke.py --url http://localhost:8100 --access-code ...

Watch the demo rate limits (api.md §1): 60 messages per hour per IP, so one run of 50 tickets
fits, two in the same hour do not.
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
import uuid
from dataclasses import dataclass

import httpx2

SEPA = (
    "Hi, I withdrew 420 EUR to my bank account by SEPA yesterday afternoon and it is still not "
    "there. How long does a SEPA withdrawal take?"
)
FINAL = {"answered", "pending_approval", "handed_off", "closed"}


@dataclass
class Result:
    ticket_id: str | None
    latency_ms: float
    error: str | None


async def customer_client(url: str, access_code: str, ref: str) -> httpx2.AsyncClient:
    client = httpx2.AsyncClient(base_url=url, timeout=httpx2.Timeout(120))
    response = await client.post(
        "/api/v1/demo/session", json={"customer_ref": ref, "access_code": access_code}
    )
    response.raise_for_status()
    # The session cookie is `Secure`; over plain http to localhost the client would not send
    # it back, so it is set as a header.
    client.headers["Cookie"] = f"hub_customer={response.cookies['hub_customer']}"
    return client


async def one_ticket(client: httpx2.AsyncClient) -> Result:
    started = time.perf_counter()
    ticket_id = None
    done = False
    try:
        async with client.stream(
            "POST",
            "/api/v1/tickets",
            json={"message": SEPA},
            headers={"Idempotency-Key": str(uuid.uuid4()), "Accept": "text/event-stream"},
        ) as response:
            if response.status_code != 200:
                await response.aread()
                return Result(None, _ms(started), f"http {response.status_code}")
            event = ""
            async for line in response.aiter_lines():
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: ") and event == "ticket.created":
                    ticket_id = json.loads(line[6:])["ticket_id"]
                elif line.startswith("data: ") and event == "done":
                    done = True
        return Result(ticket_id, _ms(started), None if done else "stream ended without done")
    except httpx2.HTTPError as error:
        return Result(ticket_id, _ms(started), type(error).__name__)


def _ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000


async def customer(url: str, code: str, ref: str, tickets: int) -> list[tuple[str, Result]]:
    client = await customer_client(url, code, ref)
    try:
        return [(ref, await one_ticket(client)) for _ in range(tickets)]
    finally:
        await client.aclose()


async def lost_runs(url: str, code: str, results: list[tuple[str, Result]]) -> list[str]:
    """Tickets still in `processing` 30 s after the load: their run was lost."""
    clients = {ref: await customer_client(url, code, ref) for ref, _ in results}
    pending = {(ref, r.ticket_id) for ref, r in results if r.ticket_id}
    deadline = time.monotonic() + 30
    while pending and time.monotonic() < deadline:
        for ref, ticket_id in list(pending):
            status = (await clients[ref].get(f"/api/v1/tickets/{ticket_id}")).json()["status"]
            if status in FINAL:
                pending.discard((ref, ticket_id))
        if pending:
            await asyncio.sleep(2)
    for client in clients.values():
        await client.aclose()
    return sorted(str(ticket_id) for _, ticket_id in pending)


def percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * share
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default="http://localhost:8100")
    parser.add_argument("--access-code", required=True)
    parser.add_argument("--customers", type=int, default=10)
    parser.add_argument("--tickets", type=int, default=50)
    args = parser.parse_args()
    refs = [f"CUS-{n:04d}" for n in range(1, args.customers + 1)]
    per = args.tickets // args.customers
    started = time.perf_counter()
    batches = await asyncio.gather(*(customer(args.url, args.access_code, r, per) for r in refs))
    wall_s = time.perf_counter() - started
    results = [item for batch in batches for item in batch]
    ok = [r.latency_ms for _, r in results if r.error is None]
    errors = [r.error for _, r in results if r.error is not None]
    lost = await lost_runs(args.url, args.access_code, results)
    print("Load smoke — fake models: model time not included")
    print(f"customers {args.customers}, tickets {len(results)}, wall time {wall_s:.1f} s")
    if ok:
        print(
            f"latency to `done`: p50 {percentile(ok, 0.5):.0f} ms, p95 {percentile(ok, 0.95):.0f}"
            f" ms, max {max(ok):.0f} ms, mean {statistics.fmean(ok):.0f} ms"
        )
    print(f"errors {len(errors)} of {len(results)} ({len(errors) / len(results):.0%}): {errors}")
    print(f"lost runs (still processing after 30 s): {len(lost)} {lost}")
    return 0 if not errors and not lost else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
