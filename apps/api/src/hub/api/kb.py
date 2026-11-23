"""Knowledge-base endpoints (docs/api.md §2, §3; P5-01, P5-02).

- `GET /help/{doc_id}`: public. One external article with its sections and their anchors, the
  target of citation links (`/help/<doc_id>#<anchor>`). Internal and unknown documents → 404,
  the same answer, so the endpoint does not tell which internal ids exist.
- `GET /console/kb/search` [operator]: hybrid search over every chunk, internal included.
- `GET /console/kb/documents/{id}` [operator]: one article, internal included, for the KB page
  (P10-04). Compliance cannot use it, like the search.
- `POST /admin/kb/reindex` [admin]: reload `data/kb` into Postgres and sync the index (or
  rebuild it with `full=true`); audited as `kb.reindexed`.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel
from qdrant_client.http.exceptions import ApiException
from sqlalchemy import select

from hub.api.dependencies import ContainerDep, RequireRole, SessionDep
from hub.api.errors import ProblemError
from hub.auth.roles import ADMIN, OPERATOR
from hub.db.models import KbChunk, KbDocument, Operator
from hub.domain.enums import ActorType
from hub.domain.kb import Visibility
from hub.guardrails.masking import mask_text
from hub.kb.index import KbIndexError
from hub.kb.loader import KbLoadError
from hub.kb.retriever import DEFAULT_TOP_K, MAX_TOP_K, SearchMode
from hub.kb.retriever import KbChunk as SearchHit
from hub.kb.sections import StoredChunk, sections
from hub.kb.service import KB_DIR, Actor, KbServices, reload_kb
from hub.kb.sparse import SparseModelError
from hub.llm.schemas import LlmUnavailableError

help_router = APIRouter(prefix="/api/v1/help", tags=["help"])
console_router = APIRouter(prefix="/api/v1/console/kb", tags=["console"])
admin_router = APIRouter(prefix="/api/v1/admin/kb", tags=["admin"])

DocId = Annotated[str, Path(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")]
# Errors of the search backends: Qdrant down or refusing, the local models missing.
_BACKEND_ERRORS = (
    ApiException,
    OSError,
    KbIndexError,
    SparseModelError,
    LlmUnavailableError,
)


class HelpSectionOut(BaseModel):
    anchor: str
    heading: str
    text: str


class HelpArticleOut(BaseModel):
    id: str
    title: str
    url: str
    updated_at: datetime
    sections: list[HelpSectionOut]


class KbDocumentOut(HelpArticleOut):
    visibility: str


class SearchHitOut(BaseModel):
    chunk_id: str
    doc_id: str
    title: str
    section: str
    url: str
    visibility: str
    text: str
    score: float
    rank: int


class SearchOut(BaseModel):
    items: list[SearchHitOut]


class ReindexOut(BaseModel):
    full: bool
    collection: str
    documents_added: int
    documents_updated: int
    documents_removed: int
    chunks_embedded: int
    chunks_payload_updated: int
    chunks_deleted: int


def _kb(services: KbServices | None) -> KbServices:
    if services is None:
        raise ProblemError(503, "kb_unavailable", "Knowledge-base search is not configured.")
    return services


async def _article(session: SessionDep, doc_id: str, *, internal: bool) -> KbDocumentOut:
    document = await session.get(KbDocument, doc_id)
    if document is None or (document.visibility != Visibility.EXTERNAL and not internal):
        raise ProblemError(404, "not_found", "Article not found.")
    chunks = await session.scalars(
        select(KbChunk).where(KbChunk.doc_id == doc_id).order_by(KbChunk.ordinal)
    )
    stored = [StoredChunk(chunk.id, chunk.section, chunk.text) for chunk in chunks]
    return KbDocumentOut(
        id=document.id,
        title=document.title,
        url=document.url,
        visibility=str(document.visibility),
        updated_at=document.updated_at,
        sections=[
            HelpSectionOut(anchor=section.anchor, heading=section.heading, text=section.text)
            for section in sections(document.id, document.title, stored)
        ],
    )


@help_router.get("/{doc_id}")
async def get_help_article(doc_id: DocId, session: SessionDep) -> HelpArticleOut:
    article = await _article(session, doc_id, internal=False)
    # The public answer never says an article is internal: the field is left out.
    return HelpArticleOut(**article.model_dump(exclude={"visibility"}))


@console_router.get("/documents/{doc_id}")
async def get_kb_document(
    doc_id: DocId,
    _operator: Annotated[Operator, Depends(RequireRole(OPERATOR))],
    session: SessionDep,
) -> KbDocumentOut:
    return await _article(session, doc_id, internal=True)


@console_router.get("/search")
async def search_kb(
    _operator: Annotated[Operator, Depends(RequireRole(OPERATOR))],
    container: ContainerDep,
    q: Annotated[str, Query(min_length=1, max_length=500, pattern=r"^[^\x00]*$")],
    mode: SearchMode = "hybrid",
    limit: Annotated[int, Query(ge=1, le=MAX_TOP_K)] = DEFAULT_TOP_K,
) -> SearchOut:
    services = _kb(container.kb)
    # The query goes to the embedding model: card data is masked first (G1), as everywhere.
    query = mask_text(q)
    try:
        hits = await services.retriever.search(query, "all", top_k=limit, mode=mode)
    except _BACKEND_ERRORS:
        raise ProblemError(
            503, "kb_unavailable", "Knowledge-base search is not available now."
        ) from None
    return SearchOut(items=[_hit_out(hit) for hit in hits])


def _hit_out(hit: SearchHit) -> SearchHitOut:
    return SearchHitOut(**hit.model_dump())


@admin_router.post("/reindex")
async def reindex_kb(
    operator: Annotated[Operator, Depends(RequireRole(ADMIN))],
    container: ContainerDep,
    full: bool = False,
) -> ReindexOut:
    services = _kb(container.kb)
    if container.database is None:
        raise ProblemError(503, "kb_unavailable", "The database is not configured.")
    try:
        report = await reload_kb(
            container.database,
            services.index,
            KB_DIR,
            full=full,
            audit_as=Actor(ActorType.OPERATOR, operator.id),
        )
    except KbLoadError as error:
        # The message lists problems in our own files (data/kb), never customer data.
        raise ProblemError(422, "kb_invalid", str(error)) from None
    except _BACKEND_ERRORS:
        raise ProblemError(
            503, "kb_unavailable", "The knowledge-base index is not available now."
        ) from None
    return ReindexOut(
        full=report.full,
        collection=report.index.collection,
        documents_added=report.load.documents_added,
        documents_updated=report.load.documents_updated,
        documents_removed=report.load.documents_removed,
        chunks_embedded=report.index.embedded,
        chunks_payload_updated=report.index.payload_updated,
        chunks_deleted=report.index.deleted,
    )
