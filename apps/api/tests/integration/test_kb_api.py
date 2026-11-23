"""Knowledge-base endpoints against Postgres, Qdrant and the local model (P5-01, P5-02):
`GET /help/{doc_id}`, `GET /console/kb/search`, `POST /admin/kb/reindex`."""

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from qdrant_client import AsyncQdrantClient

from hub.db.session import connect
from hub.deps import Container
from hub.kb.retriever import KbChunk
from hub.kb.service import EMBEDDING_CALL, KbServices, build_kb_services
from hub.llm.gateway import build_gateway, embedder
from hub.llm.recorder import DbCallRecorder
from hub.main import create_app

from . import _api
from ._api import Urls, add_operator, operator_client, run_sql
from ._kb import MODEL_DIR, settings

DATABASE = "hub_kb_api_test"
TEST_CARD = "4111 1111 1111 1111"  # public test card number
EMAILS = {
    "operator": "op@example.com",
    "compliance": "co@example.com",
    "admin": "ad@example.com",
}


@pytest.fixture(scope="module")
def urls(postgres_url: str) -> Iterator[Urls]:
    if not MODEL_DIR.is_dir():
        pytest.fail(f"the local embedding model is not in {MODEL_DIR}: run `make models`")
    with _api.own_database(postgres_url, DATABASE) as found:
        yield found


@pytest.fixture
async def kb(urls: Urls, qdrant_url: str) -> AsyncIterator[KbServices]:
    run_sql(urls, "TRUNCATE audit_log, llm_calls, kb_chunks, kb_documents, operators CASCADE")
    client = AsyncQdrantClient(url=qdrant_url, check_compatibility=False)
    for item in (await client.get_collections()).collections:
        await client.delete_collection(item.name)
    await client.close()
    config = settings(urls, qdrant_url)
    database = connect(urls[1])
    gateway = build_gateway(config, DbCallRecorder(database), None)
    services = build_kb_services(config, embedder(gateway, EMBEDDING_CALL))
    yield services
    await services.close()
    await database.dispose()


@pytest.fixture
def operators(urls: Urls, kb: KbServices) -> dict[str, Any]:
    return {who: add_operator(urls, email, who) for who, email in EMAILS.items()}


@pytest.fixture
def app(
    urls: Urls, qdrant_url: str, kb: KbServices, operators: dict[str, Any]
) -> Iterator[FastAPI]:
    base = _api.container(urls)
    container = Container(
        settings=settings(urls, qdrant_url).model_copy(
            update={"session_secret": base.settings.session_secret}
        ),
        database=base.database,
        kb=kb,
    )
    app = create_app(container)
    with TestClient(app):
        yield app


@pytest.fixture
def staff(app: FastAPI) -> dict[str, TestClient]:
    return {who: operator_client(app, email) for who, email in EMAILS.items()}


@pytest.fixture
def indexed(staff: dict[str, TestClient]) -> dict[str, Any]:
    response = staff["admin"].post("/api/v1/admin/kb/reindex")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# --- POST /admin/kb/reindex ----------------------------------------------------------------------


def test_admin_reindex_loads_postgres_and_qdrant_and_is_audited(
    indexed: dict[str, Any], staff: dict[str, TestClient], urls: Urls, operators: dict[str, Any]
) -> None:
    assert indexed["full"] is False
    assert indexed["collection"] == "kb_chunks_v1"
    assert indexed["documents_added"] > 50
    total = run_sql(urls, "SELECT count(*) FROM kb_chunks")[0][0]
    assert indexed["chunks_embedded"] == total
    again = staff["admin"].post("/api/v1/admin/kb/reindex").json()
    assert (again["documents_added"], again["chunks_embedded"]) == (0, 0)
    full = staff["admin"].post("/api/v1/admin/kb/reindex", params={"full": "true"}).json()
    assert (full["full"], full["collection"], full["chunks_embedded"]) == (
        True, "kb_chunks_v2", total,
    )  # fmt: skip
    rows = run_sql(
        urls,
        "SELECT actor_type, actor_id, details->>'full' FROM audit_log"
        " WHERE action = 'kb.reindexed' ORDER BY id",
    )
    admin = str(operators["admin"])
    assert rows == [("operator", admin, "false")] * 2 + [("operator", admin, "true")]


@pytest.mark.parametrize("who", ["operator", "compliance"])
def test_only_an_admin_may_reindex(staff: dict[str, TestClient], who: str) -> None:
    response = staff[who].post("/api/v1/admin/kb/reindex")
    assert (response.status_code, response.json()["code"]) == (403, "forbidden")


def test_reindex_needs_the_csrf_token(app: FastAPI, staff: dict[str, TestClient]) -> None:
    admin = staff["admin"]
    del admin.headers["X-CSRF-Token"]
    response = admin.post("/api/v1/admin/kb/reindex")
    assert (response.status_code, response.json()["code"]) == (403, "csrf_failed")


# --- GET /help/{doc_id} --------------------------------------------------------------------------


def test_help_shows_an_external_article_with_section_anchors(
    app: FastAPI, indexed: dict[str, Any], staff: dict[str, TestClient]
) -> None:
    response = _api.client(app).get("/api/v1/help/withdrawal-times")
    assert response.status_code == 200
    article = response.json()
    assert article["title"] == "How long does a withdrawal take?"
    assert article["url"] == "/help/withdrawal-times"
    anchors = [section["anchor"] for section in article["sections"]]
    assert anchors[0] == "sepa-withdrawals"
    assert article["sections"][0]["text"].startswith("SEPA is for EUR withdrawals")
    # Every search hit of this article links to a section the page has.
    hits = staff["operator"].get("/api/v1/console/kb/search", params={"q": "SEPA withdrawal"})
    urls = {hit["url"] for hit in hits.json()["items"] if hit["doc_id"] == "withdrawal-times"}
    assert urls
    assert {url.partition("#")[2] for url in urls} <= set(anchors)


def test_help_shows_error_codes_by_code(app: FastAPI, indexed: dict[str, Any]) -> None:
    article = _api.client(app).get("/api/v1/help/error-codes").json()
    assert "err-2003" in {section["anchor"] for section in article["sections"]}


@pytest.mark.parametrize("doc_id", ["internal-fraud-checklist", "no-such-article"])
def test_help_hides_internal_and_unknown_articles_alike(
    app: FastAPI, indexed: dict[str, Any], doc_id: str
) -> None:
    response = _api.client(app).get(f"/api/v1/help/{doc_id}")
    assert (response.status_code, response.json()["code"]) == (404, "not_found")


def test_help_never_says_whether_an_article_is_internal(
    app: FastAPI, indexed: dict[str, Any]
) -> None:
    article = _api.client(app).get("/api/v1/help/withdrawal-times").json()
    assert "visibility" not in article


# --- GET /console/kb/documents/{id} (P10-04) -----------------------------------------------------


def test_the_console_shows_internal_articles_to_operators_only(
    indexed: dict[str, Any], staff: dict[str, TestClient]
) -> None:
    response = staff["operator"].get("/api/v1/console/kb/documents/internal-fraud-checklist")
    assert response.status_code == 200, response.text
    article = response.json()
    assert article["visibility"] == "internal"
    assert article["sections"]
    external = staff["operator"].get("/api/v1/console/kb/documents/withdrawal-times").json()
    assert external["visibility"] == "external"
    missing = staff["operator"].get("/api/v1/console/kb/documents/no-such-article")
    assert missing.status_code == 404
    denied = staff["compliance"].get("/api/v1/console/kb/documents/internal-fraud-checklist")
    assert (denied.status_code, denied.json()["code"]) == (403, "forbidden")


def test_the_console_article_needs_a_session(app: FastAPI, indexed: dict[str, Any]) -> None:
    response = _api.client(app).get("/api/v1/console/kb/documents/withdrawal-times")
    assert response.status_code == 401


# --- GET /console/kb/search ----------------------------------------------------------------------


def test_console_search_includes_internal_articles(
    indexed: dict[str, Any], staff: dict[str, TestClient]
) -> None:
    response = staff["operator"].get(
        "/api/v1/console/kb/search",
        params={"q": "fraud checklist for a suspicious card payment", "limit": 10},
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 10
    assert "internal" in {item["visibility"] for item in items}
    assert [item["rank"] for item in items] == list(range(1, 11))


def test_console_search_finds_an_error_code_first(
    indexed: dict[str, Any], staff: dict[str, TestClient]
) -> None:
    for mode, first in (("hybrid", True), ("dense", None)):
        response = staff["operator"].get(
            "/api/v1/console/kb/search", params={"q": "what is ERR-2003", "mode": mode}
        )
        assert response.status_code == 200
        if first:
            assert response.json()["items"][0]["chunk_id"] == "error-codes#ERR-2003"


def test_console_search_masks_card_numbers_before_embedding(
    indexed: dict[str, Any],
    staff: dict[str, TestClient],
    kb: KbServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    search = kb.retriever.search

    async def spy(query: str, *args: Any, **kwargs: Any) -> list[KbChunk]:  # noqa: ANN401
        seen.append(query)
        return await search(query, *args, **kwargs)

    monkeypatch.setattr(kb.retriever, "search", spy)
    response = staff["operator"].get(
        "/api/v1/console/kb/search", params={"q": f"card {TEST_CARD} declined"}
    )
    assert response.status_code == 200
    (query,) = seen
    assert TEST_CARD not in query
    assert "4111111111111111" not in query.replace(" ", "")


def test_console_search_rules(app: FastAPI, staff: dict[str, TestClient]) -> None:
    assert _api.client(app).get("/api/v1/console/kb/search?q=x").status_code == 401
    compliance = staff["compliance"].get("/api/v1/console/kb/search", params={"q": "x"})
    assert (compliance.status_code, compliance.json()["code"]) == (403, "forbidden")
    bad: list[dict[str, Any]] = [
        {"q": ""},
        {"q": "x", "limit": 0},
        {"q": "x", "limit": 21},
        {"q": "x", "mode": "rerank"},
    ]
    for params in bad:
        response = staff["operator"].get("/api/v1/console/kb/search", params=params)
        assert response.status_code == 422, params


def test_kb_endpoints_answer_503_without_search(urls: Urls, operators: dict[str, Any]) -> None:
    app = create_app(_api.container(urls))
    with TestClient(app):
        operator = operator_client(app, EMAILS["admin"])
        search = operator.get("/api/v1/console/kb/search", params={"q": "x"})
        reindex = operator.post("/api/v1/admin/kb/reindex")
    assert [(r.status_code, r.json()["code"]) for r in (search, reindex)] == [
        (503, "kb_unavailable")
    ] * 2
