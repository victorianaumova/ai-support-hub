"""The integration setup itself: containers start, the readiness checks pass against them, and
the compose file keeps its rules (pinned images, ports on localhost only)."""

import re

import psycopg
import pytest

from hub.kb.index import connect_qdrant
from hub.observability.readiness import libpq_url, postgres_check, qdrant_check

from ._compose import compose_config, compose_image

PINNED_TAG = re.compile(r":v?\d+\.\d+(\.\d+)?(-[a-z0-9.]+)?$")


async def test_postgres_check_passes_against_the_container(postgres_url: str) -> None:
    await postgres_check(postgres_url)()


async def test_postgres_runs_the_compose_version(postgres_url: str) -> None:
    major_minor = compose_image("postgres").split(":")[1].split("-")[0]
    async with await psycopg.AsyncConnection.connect(libpq_url(postgres_url)) as connection:
        cursor = await connection.execute("SHOW server_version")
        row = await cursor.fetchone()
    assert row is not None
    assert row[0].startswith(major_minor)


async def test_init_script_creates_the_eval_database(postgres_url: str) -> None:
    async with await psycopg.AsyncConnection.connect(libpq_url(postgres_url)) as connection:
        cursor = await connection.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", ("hub_eval",)
        )
        assert await cursor.fetchone() is not None


async def test_qdrant_check_passes_against_the_container(qdrant_url: str) -> None:
    client = connect_qdrant(qdrant_url, "")
    await qdrant_check(client.get_aliases)()


@pytest.mark.parametrize("service", ["postgres", "qdrant"])
def test_compose_images_are_pinned(service: str) -> None:
    assert PINNED_TAG.search(compose_image(service)), f"{service} image is not pinned to a version"


def test_compose_ports_bind_to_localhost_only() -> None:
    services = compose_config()["services"]
    published = [
        (name, port) for name, service in services.items() for port in service.get("ports", [])
    ]
    assert published, "no published ports found"
    for name, port in published:
        assert port.get("host_ip") == "127.0.0.1", f"{name} publishes {port} beyond localhost"
