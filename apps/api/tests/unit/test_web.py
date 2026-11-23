"""The built web app is served for page paths; API paths never fall back to the page (P10-06)."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub.api.errors import install_error_handlers
from hub.api.web import is_reserved, mount_web

INDEX = "<!doctype html><title>hub</title>"


@pytest.mark.parametrize(
    "path",
    [
        "api",
        "api/",
        "api/tickets/x",
        "health",
        "ready",
        "docs",
        "docs/oauth2-redirect",
        "redoc",
        "openapi.json",
    ],
)
def test_reserved_paths(path: str) -> None:
    assert is_reserved(path)


@pytest.mark.parametrize(
    "path", ["", "healthcheck", "ready-x", "readyz", "apis", "api-docs", "documents", "chat"]
)
def test_page_paths_are_not_reserved(path: str) -> None:
    assert not is_reserved(path)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    (tmp_path / "index.html").write_text(INDEX)
    (tmp_path / "favicon.svg").write_text("<svg/>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    app = FastAPI()
    install_error_handlers(app)
    mount_web(app, tmp_path)
    return TestClient(app)


@pytest.mark.parametrize("path", ["/", "/chat", "/console/queue", "/healthcheck", "/ready-x"])
def test_pages_answer_index(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.text == INDEX
    assert response.headers["cache-control"] == "no-cache"


@pytest.mark.parametrize("path", ["/api/nope", "/health", "/ready", "/docs/x"])
def test_reserved_paths_are_json_404(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"


def test_top_level_file_and_assets(client: TestClient) -> None:
    assert client.get("/favicon.svg").text == "<svg/>"
    assert client.get("/assets/app.js").text == "console.log(1)"


def test_no_escape_from_the_build_folder(client: TestClient) -> None:
    assert client.get("/..%2f..%2fetc%2fpasswd").text == INDEX


def test_missing_index_fails_fast(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match=r"no index\.html"):
        mount_web(FastAPI(), tmp_path)
