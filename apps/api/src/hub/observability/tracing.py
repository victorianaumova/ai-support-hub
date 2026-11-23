"""Run traces in Langfuse (ADR-0014, restored by ADR-0021; operations §3), P9-01.

One trace per run. Its id is derived from the run id (`Langfuse.create_trace_id(seed=run id)`),
so a run resumed after the operator's decision, or recovered after a restart, adds to the same
trace; `runs.trace_id` stores it. Inside it: one span per graph node, one generation per model
call attempt, one tool observation per tool call, one retriever observation per knowledge-base
search.

**G9 (security §3).** Every value that goes into a trace passes `trace_safe` first: card data
masked with the G1 function, and any key that names a restriction reason dropped with its value.
The same function is Langfuse's `mask` hook, so nothing set through the SDK skips it.

**Off when keys are missing.** Without `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` the
tracer has no client: every method is a no-op, no thread starts and nothing goes over the
network. Tracing never breaks a run: a failure inside the SDK is logged with its class only.

**Sampling.** Every online run is traced. An evaluation run is traced when a stable hash of its
run id falls below `EVAL_TRACE_SAMPLE`, so a run and its resume make the same choice.

**Only inside a traced run.** Observations are made only while a run trace is open (a context
variable). A model or tool call outside a run (knowledge-base indexing, the memory summary when a
ticket is closed, the console's Customer tab, evaluation judges) is not traced.

Langfuse gets a `TracerProvider` of its own, never the global one, so the spans of other
OpenTelemetry users in the process do not reach it.
"""

import hashlib
import logging
import uuid
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from langfuse import Langfuse, propagate_attributes
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter
from pydantic import BaseModel

from hub.config import Settings
from hub.guardrails.masking import mask_text

logger = logging.getLogger(__name__)

DEFAULT_HOST = "https://cloud.langfuse.com"
# Keys whose values never go into a trace, compared without case, spaces or punctuation: the
# tipping-off rule (world file §6). The Hub never holds a restriction reason; this is the second
# line behind the tool results, which drop it field by field.
DROPPED_KEY_PARTS = ("restrictionreason",)
# Langfuse limits a propagated attribute value to 200 characters.
_MAX_ATTRIBUTE = 200

ObservationType = Literal["span", "generation", "embedding", "tool", "retriever", "guardrail"]
Level = Literal["DEBUG", "DEFAULT", "WARNING", "ERROR"]

_in_traced_run: ContextVar[bool] = ContextVar("hub_in_traced_run", default=False)


def _dropped(key: object) -> bool:
    folded = "".join(char for char in str(key).lower() if char.isalnum())
    return any(part in folded for part in DROPPED_KEY_PARTS)


def trace_safe(value: object) -> Any:  # noqa: ANN401 - JSON-like data of any shape
    """`value` as it may go into a trace (G9): strings card-masked, restriction reasons dropped,
    everything else turned into JSON-like data."""
    if value is None or isinstance(value, bool | float):
        return value
    if isinstance(value, int):
        # A card number given as an int must not slip through.
        text = str(value)
        masked = mask_text(text)
        return value if masked == text else masked
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, Enum):
        return trace_safe(value.value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, BaseModel):
        return trace_safe(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {
            mask_text(str(key)): trace_safe(item)
            for key, item in value.items()
            if not _dropped(key)
        }
    if isinstance(value, list | tuple | set | frozenset):
        return [trace_safe(item) for item in value]
    return mask_text(str(value))


def _mask_hook(*, data: Any, **_kwargs: Any) -> Any:  # noqa: ANN401 - Langfuse's MaskFunction
    return trace_safe(data)


# Fields of an observation that can carry text; the others are numbers, names and levels.
_TEXT_FIELDS = ("input", "output", "metadata", "status_message")


def _safe_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {
        name: trace_safe(value) if name in _TEXT_FIELDS else value
        for name, value in fields.items()
        if value is not None
    }


def _attribute(value: object) -> str:
    return mask_text(str(value))[:_MAX_ATTRIBUTE]


class Observation:
    """One span, generation or tool call in a trace; does nothing when its run is not traced."""

    def __init__(self, inner: Any = None) -> None:  # noqa: ANN401 - a Langfuse observation
        self._inner = inner

    @property
    def recording(self) -> bool:
        return self._inner is not None

    def update(self, **fields: Any) -> None:  # noqa: ANN401 - Langfuse's update fields
        if self._inner is None:
            return
        try:
            self._inner.update(**_safe_fields(fields))
        except Exception as exc:
            logger.warning("trace update failed", extra={"error": type(exc).__name__})

    def end(self, **fields: Any) -> None:  # noqa: ANN401 - Langfuse's update fields
        """Update, then close an observation made with `Tracer.start`."""
        if self._inner is None:
            return
        self.update(**fields)
        try:
            self._inner.end()
        except Exception as exc:
            logger.warning("trace end failed", extra={"error": type(exc).__name__})


def trace_url(host: str, project_id: str, trace_id: str | None) -> str | None:
    """The trace's page in the Langfuse UI; None without a trace or a project id."""
    if not trace_id or not project_id:
        return None
    return f"{(host or DEFAULT_HOST).rstrip('/')}/project/{project_id}/traces/{trace_id}"


def sampled(run_id: uuid.UUID, context: str, eval_sample: float) -> bool:
    """Every run except evaluation runs; those by a stable hash of the run id."""
    if context != "eval":
        return True
    digest = hashlib.sha256(str(run_id).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64 < eval_sample


@dataclass(frozen=True)
class Tracer:
    """The process's tracer. `Tracer()` is off; `from_settings` turns it on when keys are set."""

    client: Langfuse | None = None
    eval_sample: float = 0.0

    @classmethod
    def from_settings(
        cls, settings: Settings, span_exporter: SpanExporter | None = None
    ) -> "Tracer":
        """`span_exporter`: tests pass an in-memory exporter, so nothing goes over the network."""
        public_key = settings.langfuse_public_key
        secret_key = settings.langfuse_secret_key.get_secret_value()
        if not public_key or not secret_key:
            return cls()
        client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            base_url=settings.langfuse_host or DEFAULT_HOST,
            environment=settings.app_env,
            tracer_provider=TracerProvider(),
            mask=_mask_hook,
            span_exporter=span_exporter,
        )
        return cls(client=client, eval_sample=settings.eval_trace_sample)

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def trace_id(self, run_id: uuid.UUID, context: str) -> str | None:
        """The run's trace id, or None when the run is not traced."""
        if self.client is None or not sampled(run_id, context, self.eval_sample):
            return None
        return Langfuse.create_trace_id(seed=str(run_id))

    @contextmanager
    def run(
        self,
        run_id: uuid.UUID,
        *,
        ticket_id: uuid.UUID,
        context: str,
        name: str,
        attributes: Mapping[str, str],
    ) -> Iterator[Observation]:
        """The run's root span, current for the nodes inside. `attributes`: ids and versions
        only (they are set on every observation of the trace)."""
        trace_id = self.trace_id(run_id, context)
        if trace_id is None or self.client is None:
            yield Observation()
            return
        stack = ExitStack()
        try:
            root = stack.enter_context(
                self.client.start_as_current_observation(
                    trace_context={"trace_id": trace_id},
                    name=name,
                    as_type="span",
                    metadata=trace_safe(dict(attributes)),
                )
            )
            stack.enter_context(
                propagate_attributes(
                    session_id=str(ticket_id),
                    trace_name="run",
                    tags=[context],
                    metadata={key: _attribute(value) for key, value in attributes.items()},
                )
            )
        except Exception as exc:
            stack.close()
            logger.warning("trace not started", extra={"error": type(exc).__name__})
            yield Observation()
            return
        token = _in_traced_run.set(True)
        try:
            yield Observation(root)
        finally:
            _in_traced_run.reset(token)
            try:
                stack.close()
            except Exception as exc:
                logger.warning("trace not closed", extra={"error": type(exc).__name__})

    @contextmanager
    def observe(
        self,
        name: str,
        *,
        as_type: ObservationType = "span",
        **fields: Any,  # noqa: ANN401 - Langfuse's observation fields
    ) -> Iterator[Observation]:
        """An observation that is current inside the block (a node: its calls nest under it)."""
        if self.client is None or not _in_traced_run.get():
            yield Observation()
            return
        stack = ExitStack()
        try:
            inner = stack.enter_context(
                self.client.start_as_current_observation(
                    name=name, as_type=as_type, **_safe_fields(fields)
                )
            )
        except Exception as exc:
            stack.close()
            logger.warning("trace span not started", extra={"error": type(exc).__name__})
            yield Observation()
            return
        try:
            yield Observation(inner)
        finally:
            try:
                stack.close()
            except Exception as exc:
                logger.warning("trace span not closed", extra={"error": type(exc).__name__})

    def start(self, name: str, *, as_type: ObservationType, **fields: Any) -> Observation:  # noqa: ANN401
        """An observation the caller ends (`Observation.end`): a model or tool call."""
        if self.client is None or not _in_traced_run.get():
            return Observation()
        try:
            return Observation(
                self.client.start_observation(name=name, as_type=as_type, **_safe_fields(fields))
            )
        except Exception as exc:
            logger.warning("trace observation not started", extra={"error": type(exc).__name__})
            return Observation()

    def flush(self) -> None:
        """Send what is buffered (the end of an evaluation run, the app's shutdown)."""
        if self.client is None:
            return
        try:
            self.client.flush()
        except Exception as exc:
            logger.warning("trace flush failed", extra={"error": type(exc).__name__})
