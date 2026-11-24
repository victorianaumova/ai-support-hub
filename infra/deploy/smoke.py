"""Post-deploy smoke check (operations §6, P10-07): `/ready` must answer 200, then (with
`--canary`) one fixed customer question goes through the real pipeline and its stream must end
with `ticket.status` and `done`. The canary costs under one cent; it is the only real model call
any workflow makes (operations §5). Exit 1 on any failure, which makes deploy.yml roll back.

The canary must not be a repeat contact (`memory_shows_open_case`, ai-pipeline §6), or every
canary after the first would go to approval for that reason alone (P12). It asks as the first
demo customer with no open ticket and no ticket from the last 17 days (the 14-day memory window
plus the 72 hours after which `close-stale` closes an answered ticket and writes its memory);
the customer API shows no intent, so any ticket counts. Its own ticket keeps that customer out
of the next canaries for 17 days. The route it got must be `auto_reply` or `approval`
(`ticket.status` `answered` or `pending_approval`): a handoff means the pipeline produced no
answer the policy lets through, which a fixed help-centre question never needs.

    DEMO_ACCESS_CODE=... uv run python infra/deploy/smoke.py --url https://... --canary
"""

import argparse
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx2

CANARY = "How long does a SEPA withdrawal take?"
# ai-pipeline §9 memory window (14 days) + the close-stale delay (72 hours).
CLEAN_FOR = timedelta(days=17)
# `ticket.status` of the routes the canary may get: auto_reply and approval.
EXPECTED_STATUSES = frozenset({"answered", "pending_approval"})


def ready(client: httpx2.Client) -> None:
    # A cold start (scale to zero, Neon waking up) can take a while: retry for two minutes.
    deadline = time.monotonic() + 120
    while True:
        response = client.get("/ready")
        if response.status_code == 200:
            print("ready:", response.json())
            return
        if time.monotonic() > deadline:
            raise SystemExit(f"/ready answered {response.status_code}: {response.text[:300]}")
        time.sleep(5)


def login(client: httpx2.Client, access_code: str, customer_ref: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/demo/session", json={"customer_ref": customer_ref, "access_code": access_code}
    )
    response.raise_for_status()
    return {"Cookie": f"hub_customer={response.cookies['hub_customer']}"}


def is_clean(tickets: list[dict[str, str]], now: datetime) -> bool:
    """No open ticket and no ticket of the last 17 days (module docstring)."""
    return all(
        ticket["status"] == "closed"
        and now - datetime.fromisoformat(ticket["created_at"]) > CLEAN_FOR
        for ticket in tickets
    )


def clean_customer(client: httpx2.Client, access_code: str) -> tuple[str, dict[str, str]]:
    """The first demo customer the canary may ask as, with its session cookie."""
    listed = client.get("/api/v1/demo/customers", headers={"X-Demo-Access-Code": access_code})
    listed.raise_for_status()
    now = datetime.now(UTC)
    for customer in listed.json():
        cookie = login(client, access_code, customer["customer_ref"])
        tickets = client.get("/api/v1/tickets", params={"limit": 100}, headers=cookie)
        tickets.raise_for_status()
        page = tickets.json()
        # More than 100 tickets: not worth paging through, take the next customer.
        if page["next_cursor"] is None and is_clean(page["items"], now):
            return customer["customer_ref"], cookie
    raise SystemExit(
        "no demo customer without an open or recent ticket: the canary would be a repeat contact"
    )


def stream_events(lines: list[str]) -> list[tuple[str, str]]:
    """(event, data) pairs of a server-sent event stream: one per block of lines ended by an
    empty line, in whatever order its fields come; comments (": ping") are skipped."""
    events: list[tuple[str, str]] = []
    name, data = "", ""
    for line in [*lines, ""]:
        if line == "":
            if name:
                events.append((name, data))
            name, data = "", ""
        elif line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: "):
            data = line[6:]
    return events


def check_stream(events: list[tuple[str, str]]) -> str:
    """The ticket status the canary stream ended with; SystemExit when it is not a good end."""
    names = [name for name, _ in events]
    print("canary events:", names)
    if "ticket.status" not in names or names[-1:] != ["done"]:
        raise SystemExit("the canary stream did not end with ticket.status and done")
    if "error" in names:
        raise SystemExit("the canary run failed (error event)")
    status = str(json.loads(dict(events)["ticket.status"])["status"])
    if status not in EXPECTED_STATUSES:
        raise SystemExit(f"the canary got ticket status {status}, expected auto_reply or approval")
    return status


def canary(client: httpx2.Client, access_code: str) -> None:
    customer_ref, cookie = clean_customer(client, access_code)
    print("canary customer:", customer_ref)
    with client.stream(
        "POST",
        "/api/v1/tickets",
        json={"message": CANARY},
        headers={**cookie, "Idempotency-Key": str(uuid.uuid4()), "Accept": "text/event-stream"},
    ) as response:
        response.raise_for_status()
        events = stream_events(list(response.iter_lines()))
    print("canary status:", check_stream(events))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--canary", action="store_true")
    args = parser.parse_args()
    with httpx2.Client(base_url=args.url, timeout=httpx2.Timeout(120)) as client:
        ready(client)
        if args.canary:
            canary(
                client, os.environ["DEMO_ACCESS_CODE"].strip()
            )  # a pasted secret may end in "\n"
    print("smoke: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
