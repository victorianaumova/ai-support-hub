"""Checks the database roles of a deployment (P10-07 done-when): the app user may read and
write tickets but cannot change or delete `audit_log`.

    DATABASE_URL=<app user URL> uv run python infra/deploy/check_roles.py

Nothing is written: every statement runs in a transaction that is rolled back.
"""

import os
import sys

import psycopg

from hub.observability.readiness import libpq_url


def main() -> int:
    url = libpq_url(os.environ["DATABASE_URL"])
    failures = []
    with psycopg.connect(url) as connection:
        for statement in ("UPDATE audit_log SET action = action", "DELETE FROM audit_log"):
            try:
                with connection.transaction(force_rollback=True):
                    connection.execute(statement)
                failures.append(f"allowed (must be refused): {statement}")
            except psycopg.errors.InsufficientPrivilege:
                print(f"refused, as it must be: {statement}")
        with connection.transaction(force_rollback=True):
            connection.execute("SELECT count(*) FROM tickets")
            connection.execute("SELECT count(*) FROM audit_log")
        print("the app user can read tickets and audit_log")
    for failure in failures:
        print(failure)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
