"""Evidence and draft helpers of the workflow (P6-01; written for the linear run, P3-05 to P5-04).

Pure functions, apart from `writer_evidence` and `internal_text`, which read the KB.

Citations name chunk ids (B0: document ids) or account fact ids (`A1`, P7-04). The server, not
the model, turns each one into a link: the title of the chunk's article and the URL of its
section (`/help/<doc_id>#<anchor>`, from the index payload); an account fact gets the title
`Your account` and no URL (it is not a help-centre page). A citation whose id is not one of the
hits or facts is dropped with its marker, so a customer never sees a link the writer was not
given.

Model output is untrusted (security T2-T5): every model-written string (draft body, missing
information, proposed actions, triage summary) gets the run's placeholders restored and then
goes through the card mask (G1) before it is stored (AGENTS.md never-do #1). The model only ever
sees masked text, but it can still write digits, e.g. a number an injection made it spell out.
The output checks (G7, `guardrails/output_checks.py`) see the answer before this floor.
"""

import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from hub.db.models import KbChunk as KbChunkRow
from hub.db.models import KbDocument
from hub.domain.account import AccountFact
from hub.domain.draft import DraftAnswer
from hub.domain.kb import Visibility
from hub.domain.memory import CustomerContext
from hub.domain.verification import Verification
from hub.graph.nodes.intake import IntakeResult
from hub.graph.pipeline import (
    B0_VARIANT,
    SEARCH_MODES,
    SEARCH_TOP_K,
    KbUnavailableError,
    Pipeline,
)
from hub.guardrails.masking import mask
from hub.guardrails.output_checks import DraftToCheck, InternalText, prompt_text
from hub.kb.chunk_text import render_chunks
from hub.kb.full_text import KbSource, load_external_sources, render_kb
from hub.kb.retriever import KbChunk
from hub.tools.facts import render_facts

# The writer's blocks when there is nothing to put in them (write/v3).
NO_ACCOUNT_FACTS = "(none)"
NO_MEMORY = "(none)"
ACCOUNT_DOC_ID = "account"
ACCOUNT_TITLE = "Your account"


@dataclass
class OutputMask:
    """Prepares model-written text for storage: the run's placeholders are restored (G3), so
    the operator never sees a dangling `<EMAIL_1>`, then card data is masked (G1). `counts`
    sums what was masked, by type (`{"failed": n}` when masking failed); counts only, for the
    audit record."""

    intake_result: IntakeResult
    counts: Counter[str] = field(default_factory=Counter)

    def __call__(self, text: str) -> str:
        result = mask(self.intake_result.restore(text))
        self.counts.update({str(kind): count for kind, count in result.counts.items()})
        if result.failed:
            self.counts["failed"] += 1
        return result.text


_MARKER = re.compile(r" ?\[(\d+)\]")


def drop_markers(body: str, keep: set[int]) -> tuple[str, list[int]]:
    """Remove the `[n]` markers that have no kept citation (an unknown source id, or no
    citation at all), so the operator and the customer never see a marker without a source.
    Returns the body and the dropped marker numbers, in order of appearance."""
    dropped: list[int] = []

    def replace(match: re.Match[str]) -> str:
        number = int(match.group(1))
        if number in keep:
            return match.group(0)
        dropped.append(number)
        return ""

    return _MARKER.sub(replace, body), dropped


@dataclass(frozen=True, slots=True)
class Citable:
    """What a citation can point to: a chunk (RAG) or a whole document (B0)."""

    source_id: str
    doc_id: str
    title: str
    url: str


@dataclass(frozen=True)
class WriterEvidence:
    """What the writer read: the `{{kb}}` text, the ids it may cite, and the hits to store.
    `chunks` is None in B0, which runs no search."""

    variant: str
    kb: str
    citable: Mapping[str, Citable]
    chunks: tuple[KbChunk, ...] | None = None
    facts: tuple[AccountFact, ...] = ()

    @property
    def account_facts(self) -> str:
        """The `{{account_facts}}` value of write/v3."""
        return render_facts(self.facts) or NO_ACCOUNT_FACTS

    @property
    def sources(self) -> str:
        """Every source as the verifier reads it (`{{kb}}` of verify/v1): the sections, then the
        account facts in the same `Source id:` form. Without facts, the sections alone, so a
        run without tools sends the same request as before P7-04."""
        if not self.facts:
            return self.kb
        return "\n\n".join(part for part in (self.kb, render_facts(self.facts)) if part)

    def stored(self, cited: set[str], dropped: list[int]) -> dict[str, Any]:
        """`drafts.evidence` (data model): the hits of the search for the operator, or in B0
        the cited documents."""
        if self.chunks is None:
            documents = [
                {"doc_id": item.doc_id, "title": item.title, "url": item.url}
                for item in self.citable.values()
                if item.source_id in cited
            ]
            return {"variant": self.variant, "documents": documents, "dropped_markers": dropped}
        stored: dict[str, Any] = {
            "variant": self.variant,
            "chunks": [
                chunk.model_dump(include=STORED_CHUNK_FIELDS, mode="json") for chunk in self.chunks
            ],
            "dropped_markers": dropped,
        }
        if self.facts:
            stored["account_facts"] = [fact.model_dump(mode="json") for fact in self.facts]
        return stored


STORED_CHUNK_FIELDS = {"chunk_id", "doc_id", "title", "section", "url", "score", "rank", "text"}


def b0_evidence(sources: Sequence[KbSource]) -> WriterEvidence:
    return WriterEvidence(
        variant=B0_VARIANT,
        kb=render_kb(sources),
        citable={s.id: Citable(s.id, s.id, s.title, s.url) for s in sources},
    )


def rag_evidence(
    variant: str, hits: Sequence[KbChunk], facts: Sequence[AccountFact] = ()
) -> WriterEvidence:
    """The hits and account facts as the writer reads them. Only `external` chunks can be
    cited: the search filters on it, and this is the second check (AGENTS.md never-do 5)."""
    kept = tuple(hit for hit in hits if hit.visibility == "external")
    citable = {h.chunk_id: Citable(h.chunk_id, h.doc_id, h.title, h.url) for h in kept}
    citable.update(
        {f.fact_id: Citable(f.fact_id, ACCOUNT_DOC_ID, ACCOUNT_TITLE, "") for f in facts}
    )
    return WriterEvidence(
        variant=variant,
        kb=render_chunks(kept),
        citable=citable,
        chunks=kept,
        facts=tuple(facts),
    )


def memory_text(context: CustomerContext | None) -> str:
    """The `{{memory}}` value of write/v3: the memory items, newest first, context only."""
    if context is None or not context.memory:
        return NO_MEMORY
    return "\n".join(
        f"- {item.created_at.date().isoformat()} ({item.intent or 'other'}): {item.summary}"
        for item in context.memory
    )


def draft_from_answer(
    answer: DraftAnswer,
    evidence: WriterEvidence,
    clean: OutputMask,
    verification: Verification | None = None,
) -> tuple[dict[str, Any], int]:
    """The column values of the draft, and the number of citations dropped because their id is
    not one the writer was given. Every model-written string goes through `clean` (placeholders
    restored, then G1). Markers without a kept citation are removed from the body and listed in
    `evidence.dropped_markers`. The link of a citation (title, section URL) comes from the
    evidence, never from the model. With a `verification`, the draft gets its groundedness and
    the unsupported claims (model-written, so cleaned too)."""
    citations: list[dict[str, Any]] = []
    unknown = 0
    for citation in answer.citations:
        source = evidence.citable.get(citation.source_id)
        if source is None:
            unknown += 1
            continue
        citations.append(
            {
                "marker": citation.marker,
                "source_id": source.source_id,
                "doc_id": source.doc_id,
                "title": source.title,
                "url": source.url,
            }
        )
    actions = [
        {
            "id": f"a{index}",
            "type": action.type,
            "params": {
                "issue_type": action.issue_type,
                "summary": clean(action.summary),
                "description": clean(action.description),
            },
        }
        for index, action in enumerate(answer.proposed_actions, start=1)
    ]
    missing = answer.missing_information
    body, dropped = (
        drop_markers(clean(answer.answer), {citation["marker"] for citation in citations})
        if answer.can_answer
        else ("", [])
    )
    cited = {citation["source_id"] for citation in citations}
    return (
        {
            "can_answer": answer.can_answer,
            "missing_information": clean(missing) if missing else None,
            "body": body,
            "citations": citations,
            "evidence": evidence.stored(cited, dropped),
            "proposed_actions": actions,
            **verification_columns(verification, clean),
        },
        unknown,
    )


def verification_columns(
    verification: Verification | None, clean: Callable[[str], str]
) -> dict[str, Any]:
    """`drafts.groundedness` and `drafts.unsupported_claims` (data model §1)."""
    if verification is None:
        return {"groundedness": None, "unsupported_claims": []}
    groundedness = verification.groundedness
    return {
        "groundedness": Decimal(str(groundedness)) if groundedness is not None else None,
        "unsupported_claims": [
            {"claim": clean(claim.claim), "source_id": claim.source_id}
            for claim in verification.unsupported
        ],
    }


def draft_to_check(
    answer: DraftAnswer, evidence: WriterEvidence, intake_result: IntakeResult
) -> DraftToCheck:
    """The answer as the model wrote it, for the code checks: the run's placeholders restored
    (one left over is the model's own), not card masked, every marker still in."""
    others: list[tuple[str, str]] = []
    if answer.missing_information:
        others.append(("missing_information", intake_result.restore(answer.missing_information)))
    others += [
        ("proposed_action", intake_result.restore(f"{action.summary}\n{action.description}"))
        for action in answer.proposed_actions
    ]
    return DraftToCheck(
        can_answer=answer.can_answer,
        answer=intake_result.restore(answer.answer),
        citations=[(citation.marker, citation.source_id) for citation in answer.citations],
        evidence_ids=frozenset(evidence.citable),
        other_texts=others,
    )


def verify_audit_details(verification: Verification) -> dict[str, Any]:
    """`verify.done`: counts and codes, never a claim's text (it can quote the customer)."""
    return {
        "passed": verification.passed,
        "groundedness": verification.groundedness,
        "claims": len(verification.claims),
        "unsupported_claims": len(verification.unsupported),
        "policy_violations": verification.policy_violations,
        "llm_check": verification.groundedness is not None,
    }


async def search(pipeline: Pipeline, query: str) -> list[KbChunk]:
    """The first search of a RAG run (ai-pipeline §4, step 0): the masked ticket text, external
    chunks only, as the writer reads them (`rag_evidence` drops anything else). An empty result
    is not an error: the writer then has nothing to cite and answers `can_answer = false`."""
    if pipeline.retriever is None:
        raise KbUnavailableError
    mode = SEARCH_MODES[pipeline.variant]
    with pipeline.tracer.observe(
        "kb_search", as_type="retriever", input={"query": query, "mode": mode}
    ) as span:
        hits = await pipeline.retriever.search(query, "external", top_k=SEARCH_TOP_K, mode=mode)
        # Ids and scores only: the chunk texts are in the writer's prompt.
        span.update(
            output=[
                {"chunk_id": hit.chunk_id, "score": hit.score, "rank": hit.rank} for hit in hits
            ]
        )
    return list(rag_evidence(pipeline.variant, hits).chunks or ())


async def evidence_for_writer(
    pipeline: Pipeline, chunks: Sequence[KbChunk], facts: Sequence[AccountFact] = ()
) -> WriterEvidence:
    """What the writer reads. B0: every external document from Postgres. RAG: the chunks and
    account facts the research step found."""
    if pipeline.variant == B0_VARIANT:
        async with pipeline.database.sessions() as session:
            sources = await load_external_sources(session)
        if not sources:
            raise KbUnavailableError
        return b0_evidence(sources)
    return rag_evidence(pipeline.variant, chunks, facts)


async def writer_evidence(pipeline: Pipeline, query: str) -> WriterEvidence:
    """The writer's evidence for a ticket text, as a run builds it (the evaluation runner's
    estimate uses it to build the same requests)."""
    if pipeline.variant == B0_VARIANT:
        return await evidence_for_writer(pipeline, ())
    return await evidence_for_writer(pipeline, await search(pipeline, query))


def prompt_fragments(pipeline: Pipeline) -> InternalText:
    """The prompt-fragment check of the code checks (G7, P8-02): the instructions of every
    prompt the run's models get."""
    prompts = (
        pipeline.triage_prompt,
        pipeline.write_prompt,
        pipeline.verify_prompt,
        pipeline.research_prompt,
        pipeline.handoff_prompt,
        pipeline.memory_prompt,
    )
    return prompt_text(prompt.system for prompt in prompts if prompt is not None)


async def internal_text(pipeline: Pipeline) -> InternalText:
    """The internal-text check of the code checks: the chunks of the KB in Postgres, internal
    against external. Read per run, so a KB reload counts at once."""
    async with pipeline.database.sessions() as session:
        rows = (
            await session.execute(
                select(KbChunkRow.text, KbDocument.visibility).join(
                    KbDocument, KbDocument.id == KbChunkRow.doc_id
                )
            )
        ).all()
    return InternalText.build(
        internal=[text for text, visibility in rows if visibility == Visibility.INTERNAL],
        external=[text for text, visibility in rows if visibility == Visibility.EXTERNAL],
    )
