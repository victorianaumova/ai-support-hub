"""The API serves the built web app (P10-06): pages fall back to index.html, API paths do not."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub.api.web import is_reserved, mount_web
from hub.config import Settings
from hub.deps import Container
from hub.main import create_app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>app</title>")
    (dist / "assets" / "index-abc.js").write_text("console.log(1)")
    (dist / "favicon.svg").write_text("<svg/>")
    (tmp_path / "secret.txt").write_text("outside")
    settings = Settings(_env_file=None, app_env="test", web_dist_dir=dist)
    return TestClient(create_app(Container(settings=settings)))


# Reserved names match whole path segments only (P10-06 review): `/readyz` is a page, not `/ready`.
@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/chat",
        "/console/tickets/abc",
        "/help/withdrawal-times",
        "/readyz",
        "/healthcheck",
        "/openapi.jsonx",
        "/apis",
    ],
)
def test_pages_answer_with_the_app(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert "<title>app</title>" in response.text
    assert response.headers["cache-control"] == "no-cache"
    assert "script-src 'self'" in response.headers["content-security-policy"]


def test_assets_and_top_level_files_are_served(client: TestClient) -> None:
    assert client.get("/assets/index-abc.js").text == "console.log(1)"
    assert client.get("/favicon.svg").text == "<svg/>"


@pytest.mark.parametrize("path", ["/api/v1/nope", "/api/", "/api", "/docs/nope"])
def test_api_paths_never_fall_back_to_the_page(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize(
    "path", ["/../secret.txt", "/%2e%2e/secret.txt", "/assets/../../secret.txt"]
)
def test_files_outside_the_build_are_never_served(client: TestClient, path: str) -> None:
    assert "outside" not in client.get(path).text


def test_health_still_answers(client: TestClient) -> None:
    assert client.get("/health").status_code == 200


def test_without_a_build_there_is_no_page(tmp_path: Path) -> None:
    app = create_app(Container(settings=Settings(_env_file=None, app_env="test")))
    assert TestClient(app).get("/chat").status_code == 404


@pytest.mark.parametrize(
    ("path", "reserved"),
    [
        ("api", True),
        ("api/v1/x", True),
        ("health", True),
        ("ready", True),
        ("docs/oauth2-redirect", True),
        ("openapi.json", True),
        ("", False),
        ("ready-x", False),
        ("healthcheck", False),
        ("api-docs", False),
        ("documents", False),
    ],
)
def test_reserved_names_are_whole_segments(path: str, reserved: bool) -> None:
    assert is_reserved(path) is reserved


def test_a_build_without_index_fails_fast(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match=r"no index\.html"):
        mount_web(FastAPI(), tmp_path)
