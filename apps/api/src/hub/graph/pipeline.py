"""What a run needs, and the run's constants (P6-01; first built for the linear run of P3-05).

`Pipeline` is built once in the app container, or by the evaluation runner with `context = eval`
and the evaluation database. The variants are the baselines of ai-pipeline §10: `rag_hybrid`
(the default), `rag_dense` (the same search with the dense vector only) and `b0_full_kb` (no
search, the whole external KB in the system part of `write/v1`; that prompt is kept for B0 alone).
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from hub.db.session import Database
from hub.domain.enums import LlmCallContext, TicketStatus
from hub.graph.checkpoint import CheckpointStore
from hub.graph.confidence import ConfidenceRule
from hub.guardrails.policy import Policy
from hub.kb.retriever import DEFAULT_TOP_K, KbChunk, SearchMode, SearchVisibility
from hub.llm.budget import BudgetCheck
from hub.llm.config import ModelsConfig
from hub.llm.gateway import Gateway
from hub.llm.prompts import Prompt, active_prompt, load_prompt
from hub.observability.tracing import Tracer
from hub.tools.mcp_client import ToolClient

# `graph-v4`: the research tool loop and the customer profile in intake (P7-04).
# `graph-v3`: the retry loop (P6-04) and memory in `intake` and `decide` (P6-05). `graph-v2` added
# the pause in `human_review` (P6-03); `graph-v1` was the LangGraph workflow of P6-01 without it
# (the linear run was `linear-v1` to `linear-v3`).
GRAPH_VERSION = "graph-v4"
VARIANT = "rag_hybrid"
B0_VARIANT = "b0_full_kb"
# The variants this workflow can run (ai-pipeline §10), with the search mode of the RAG ones.
SEARCH_MODES: dict[str, SearchMode] = {"rag_hybrid": "hybrid", "rag_dense": "dense"}
VARIANTS = (VARIANT, "rag_dense", B0_VARIANT)
# B0 reads the whole KB from the system part of the prompt; only `write/v1` is built that way.
B0_WRITE_VERSION = "v1"
SEARCH_TOP_K = DEFAULT_TOP_K

# Friendly step names of the `run.status` event (api.md §4): no internals.
STEP_UNDERSTANDING = "understanding"
STEP_SEARCHING = "searching"
STEP_WRITING = "writing"
STEP_CHECKING = "checking"

RUN_FAILED_DETAIL = "The assistant could not prepare an answer. A support agent will reply here."

type Emit = Callable[[str, dict[str, Any]], Awaitable[None]]


class KbUnavailableError(Exception):
    """No external KB document in Postgres (`make kb-load` not run), or a RAG run without a
    search: no evidence for the writer."""


class KbSearch(Protocol):
    """`hub.kb.retriever.Retriever.search`; tests pass a fake."""

    async def search(
        self, query: str, visibility: SearchVisibility, top_k: int = ..., mode: SearchMode = ...
    ) -> list[KbChunk]: ...


def write_prompt_for(directory: Path, models: ModelsConfig, variant: str) -> Prompt:
    """The write prompt of a variant: `write/v1` for B0, the active version otherwise."""
    if variant == B0_VARIANT:
        return load_prompt(directory, "write", B0_WRITE_VERSION)
    return active_prompt(directory, models, "write")


@dataclass
class LiveRuns:
    """The runs this process is working on: a new run, a resume or a recovery. Recovery and a
    resume never take a run that is live here (another instance is ruled out by the time a
    stuck run must have waited, `hub.graph.recover`)."""

    _ids: set[uuid.UUID] = field(default_factory=set)

    def __contains__(self, run_id: object) -> bool:
        return run_id in self._ids

    def claim(self, run_id: uuid.UUID) -> bool:
        """Mark the run live; False when it already is."""
        if run_id in self._ids:
            return False
        self._ids.add(run_id)
        return True

    def release(self, run_id: uuid.UUID) -> None:
        self._ids.discard(run_id)


@dataclass(frozen=True)
class Pipeline:
    """What a run needs. Built once in the app container, or by the evaluation runner with
    `context = eval` and the evaluation database."""

    database: Database
    gateway: Gateway
    budget: BudgetCheck
    triage_prompt: Prompt
    write_prompt: Prompt
    # config/policy.yaml and config/confidence.json (ai-pipeline §6, P6-02).
    policy: Policy
    confidence_rule: ConfidenceRule
    context: LlmCallContext = LlmCallContext.ONLINE
    # `AUTO_REPLY_ALLOWED` (ADR-0019): false means `decide` never auto-replies, whatever the
    # stored `auto_reply_enabled` setting says (defence in depth behind the settings endpoint).
    auto_reply_allowed: bool = False
    variant: str = VARIANT
    # The knowledge-base search of the RAG variants. None: the research step is degraded and
    # the writer has nothing to cite.
    retriever: KbSearch | None = None
    # The verify step of the RAG variants (P5-04); B0 has none.
    verify_prompt: Prompt | None = None
    # The operator summary of a handoff (`handoff_summary`, role triage). None: handoffs get the
    # fixed notice without a summary.
    handoff_prompt: Prompt | None = None
    # The memory summary of a closed ticket (`memory_summary`, role triage, P6-05). None: a
    # close writes no memory.
    memory_prompt: Prompt | None = None
    # The research tool loop (`research/v1`, P7-04). None: research is step 0 only.
    research_prompt: Prompt | None = None
    # The guarded client of the `support-tools` MCP server (P7-03). None: intake loads no
    # profile and research runs no tool loop (unit tests, the evaluation runner until P7-06).
    tools: ToolClient | None = None
    # Where the graph keeps its checkpoints. The app and the evaluation runner pass the Postgres
    # store (`CheckpointStore.postgres`); the in-memory default is for unit tests, which start
    # no database.
    checkpoints: CheckpointStore = field(default_factory=CheckpointStore.memory)
    live: LiveRuns = field(default_factory=LiveRuns, compare=False)
    # Run traces (P9-01). The app passes the tracer its gateway and tool client use; off by
    # default (unit tests, a deployment without Langfuse keys).
    tracer: Tracer = field(default_factory=Tracer, compare=False)

    def __post_init__(self) -> None:
        if self.variant not in VARIANTS:
            raise ValueError(f"unknown variant {self.variant!r}; known: {', '.join(VARIANTS)}")
        # The two prompt shapes are not interchangeable: v1 expects whole documents in the
        # system part, later versions expect search hits in the user part.
        if (self.variant == B0_VARIANT) != (self.write_prompt.front.version == B0_WRITE_VERSION):
            raise ValueError(
                f"variant {self.variant} cannot run {self.write_prompt.version} "
                f"(B0 runs write/{B0_WRITE_VERSION}, the RAG variants a later version)"
            )
        if self.verifies and self.verify_prompt is None:
            raise ValueError(f"variant {self.variant} needs a verify prompt")

    @property
    def verifies(self) -> bool:
        """Whether runs of this variant search and verify (the RAG variants)."""
        return self.variant != B0_VARIANT

    @property
    def researches(self) -> bool:
        """Whether runs of this pipeline run the research tool loop: a RAG variant with a tool
        client and a research prompt."""
        return self.verifies and self.tools is not None and self.research_prompt is not None

    @property
    def prompt_versions(self) -> dict[str, str]:
        """node -> version without the node prefix (data model §1, `runs.prompt_versions`)."""
        prompts = [self.triage_prompt, self.write_prompt]
        if self.verifies and self.verify_prompt is not None:
            prompts.append(self.verify_prompt)
        if self.researches and self.research_prompt is not None:
            prompts.append(self.research_prompt)
        return {prompt.node: prompt.front.version for prompt in prompts}


@dataclass(frozen=True)
class RunOutcome:
    run_id: uuid.UUID | None
    ticket_status: TicketStatus | None
    failed: bool


@dataclass
class RunTasks:
    """The runs in flight. A run is a task of its own, not part of the request: a browser that
    closes the stream does not cancel it. Held by the app container; the app waits for them at
    shutdown."""

    _tasks: set[asyncio.Task[None]] = field(default_factory=set)

    def start(self, job: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(job)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def wait(self, timeout_s: float) -> None:
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout_s)


def run_failed_problem() -> dict[str, Any]:
    """Data of the `error` event (api.md §4): problem details, no internals."""
    return {
        "type": "about:blank",
        "title": "Internal Server Error",
        "status": 500,
        "detail": RUN_FAILED_DETAIL,
        "code": "internal_error",
    }
