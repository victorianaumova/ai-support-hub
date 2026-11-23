"""Post-deploy smoke check (operations §6, P10-07): `/ready` must answer 200, then (with
`--canary`) one fixed customer question goes through the real pipeline and its stream must end
with `ticket.status` and `done`. The canary costs under one cent; it is the only real model call
any workflow makes (operations §5). Exit 1 on any failure, which makes deploy.yml roll back.

    DEMO_ACCESS_CODE=... uv run python infra/deploy/smoke.py --url https://... --canary
"""

import argparse
import os
import sys
import time
import uuid

import httpx2

CANARY = "How long does a SEPA withdrawal take?"
CANARY_CUSTOMER = "CUS-0001"


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


def canary(client: httpx2.Client, access_code: str) -> None:
    login = client.post(
        "/api/v1/demo/session", json={"customer_ref": CANARY_CUSTOMER, "access_code": access_code}
    )
    login.raise_for_status()
    cookie = {"Cookie": f"hub_customer={login.cookies['hub_customer']}"}
    events: list[str] = []
    with client.stream(
        "POST",
        "/api/v1/tickets",
        json={"message": CANARY},
        headers={**cookie, "Idempotency-Key": str(uuid.uuid4()), "Accept": "text/event-stream"},
    ) as response:
        response.raise_for_status()
        events = [line[7:] for line in response.iter_lines() if line.startswith("event: ")]
    print("canary events:", events)
    if "ticket.status" not in events or events[-1:] != ["done"]:
        raise SystemExit("the canary stream did not end with ticket.status and done")
    if "error" in events:
        raise SystemExit("the canary run failed (error event)")


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
