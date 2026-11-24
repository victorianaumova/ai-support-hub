"""Post-deploy smoke check (operations §6, P10-07): `/ready` must answer 200, then (with
`--canary`) one fixed customer question goes through the real pipeline and its stream must end
with `ticket.status` and `done`. The canary costs under one cent; it is the only real model call
any workflow makes (operations §5). Exit 1 on any failure, which makes deploy.yml roll back.

The canary always asks as one fixed demo customer, `CANARY_CUSTOMER` (P12, after deploys #20 and
#21): a search for a customer without recent tickets used up one demo customer per deploy and
could never stay green. That customer is not restricted (a restricted customer is always handed
off, `customer_restricted`; deploy #17 asked as CUS-0007) and is not used in the demo script's
scenes. The route must be `auto_reply` or `approval` (`ticket.status` `answered` or
`pending_approval`): an earlier ticket of the same customer can make it a repeat contact
(`memory_shows_open_case`), which goes to approval and is fine; a handoff means the pipeline
produced no answer the policy lets through, which a fixed help-centre question never needs.

After the check the canary closes its own ticket through the console, as the demo admin
(`CANARY_OPERATOR`, password `SEED_OPERATOR_PASSWORD`), so no canary ticket waits in the queue: a
draft waiting for approval is rejected without a reply (`handed_off`), then the ticket is closed.
It never logs out: a logout ends every session of that operator. The close is best effort: a
failure (for example a password that changed without a `--reset-operator-passwords` deploy) is a
GitHub warning, not a failed check, because the revision itself passed; the ticket then stays
open, and the next canary is a repeat contact that goes to approval, which is accepted.

    DEMO_ACCESS_CODE=... SEED_OPERATOR_PASSWORD=... \
        uv run python infra/deploy/smoke.py --url https://... --canary
"""

import argparse
import json
import os
import sys
import time
import uuid

import httpx2

CANARY = "How long does a SEPA withdrawal take?"
# A demo customer that is not restricted (data/seed/customers.json) and is not in the demo
# script's scenes (docs/demo-script.md, P12-03 keeps it out).
CANARY_CUSTOMER = "CUS-0032"
# The demo admin (data/seed/operators.json): not the operator or lead the demo shows.
CANARY_OPERATOR = "sana.whitcombe@tallybridge.example"
# `ticket.status` of the routes the canary may get: auto_reply and approval.
EXPECTED_STATUSES = frozenset({"answered", "pending_approval"})
# A run that streamed `done` may still be finishing its last step; a close may meet a newer
# ticket version. Each is read again, at most this many times, a second apart.
CLOSE_ATTEMPTS = 10


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


def ticket_id(events: list[tuple[str, str]]) -> str:
    """The id of the ticket the canary created (`ticket.created`)."""
    return str(json.loads(dict(events)["ticket.created"])["ticket_id"])


def operator_login(client: httpx2.Client, password: str) -> dict[str, str]:
    """Headers of a console session as `CANARY_OPERATOR`: its cookie and CSRF token."""
    response = client.post(
        "/api/v1/auth/login", json={"email": CANARY_OPERATOR, "password": password}
    )
    response.raise_for_status()
    return {
        "Cookie": f"hub_operator={response.cookies['hub_operator']}",
        "X-CSRF-Token": str(response.json()["csrf_token"]),
    }


def close_ticket(client: httpx2.Client, headers: dict[str, str], ticket: str) -> None:
    """Close the canary's ticket; a waiting draft is rejected without a reply first."""
    for _ in range(CLOSE_ATTEMPTS):
        detail = client.get(f"/api/v1/console/tickets/{ticket}", headers=headers)
        detail.raise_for_status()
        body = detail.json()
        status, version = body["status"], body["version"]
        if status == "closed":
            return
        if status == "pending_approval" and body["draft"] is not None:
            response = client.post(
                f"/api/v1/console/drafts/{body['draft']['id']}/reject",
                json={"ticket_version": version, "reason": "other", "note": "deploy canary"},
                headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
            )
        elif status in ("answered", "handed_off"):
            response = client.post(
                f"/api/v1/console/tickets/{ticket}/close",
                json={"ticket_version": version},
                headers=headers,
            )
            if response.status_code == 200:
                return
        else:  # processing: the run is still finishing
            time.sleep(1)
            continue
        if response.status_code not in (200, 409):
            raise SystemExit(
                f"closing the canary ticket: {response.status_code} {response.text[:300]}"
            )
        time.sleep(1)
    raise SystemExit(f"the canary ticket {ticket} could not be closed")


def canary(client: httpx2.Client, access_code: str, operator_password: str) -> None:
    print("canary customer:", CANARY_CUSTOMER)
    cookie = login(client, access_code, CANARY_CUSTOMER)
    with client.stream(
        "POST",
        "/api/v1/tickets",
        json={"message": CANARY},
        headers={**cookie, "Idempotency-Key": str(uuid.uuid4()), "Accept": "text/event-stream"},
    ) as response:
        response.raise_for_status()
        events = stream_events(list(response.iter_lines()))
    status = check_stream(events)
    print("canary status:", status)
    ticket = ticket_id(events)
    try:
        close_ticket(client, operator_login(client, operator_password), ticket)
    except (SystemExit, httpx2.HTTPError) as error:
        print(f"::warning::the canary ticket {ticket} was not closed: {error}")
        return
    print("canary ticket closed:", ticket)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--canary", action="store_true")
    args = parser.parse_args()
    with httpx2.Client(base_url=args.url, timeout=httpx2.Timeout(120)) as client:
        ready(client)
        if args.canary:
            # A pasted secret may end in "\n".
            canary(
                client,
                os.environ["DEMO_ACCESS_CODE"].strip(),
                os.environ["SEED_OPERATOR_PASSWORD"],  # as the seed used it
            )
    print("smoke: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
