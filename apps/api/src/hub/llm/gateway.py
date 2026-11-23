"""The only way to a model (ADR-0003; AGENTS.md never-do 2). The only module that imports
`litellm` (import-linter contract `only-gateway-imports-litellm`).

`complete` and `embed` ask for a role; `config/models.yaml` names its model and fallback. Every
call: outbound card scan (G2) → model, with a timeout and 2 retries with backoff on transient
errors → fallback model, the same way → `LlmUnavailableError`. Every attempt writes one
`llm_calls` row with tokens, cost, latency, model and prompt version, failures included.

`complete_structured` adds the schema check (ADR-0004, `hub/llm/structured.py`): the answer
is validated with Pydantic; an invalid answer gets one repair call to the model that wrote it,
and a second invalid answer is a `StructuredOutputError`. An invalid answer is recorded with
status `invalid_output`.

`LLM_MODE=fake` swaps the provider for `FixtureProvider`, which answers from recorded responses
(`make record-llm`) and never makes a real call. Tests pass a scripted provider. Every row names
the mode (`llm_calls.mode`), so the budget guard can leave fake-mode rows out.

Evaluation runs wrap the provider in `CachingProvider`, a disk cache keyed by the request (eval
plan §4): a hit makes no provider call and is recorded with mode `fake`, because it repeats a
stored answer and nothing was paid.

Every attempt is also a generation in the run's trace (P9-01, `hub.observability.tracing`):
model, masked messages and answer, tokens, cost, prompt version and status; a G2 block is a
guardrail observation without the request text. Outside a traced run this does nothing.

The budget guard (G8, `hub/llm/budget.py`) runs before every `online` chat call, before the G2
scan and before any provider call: a refused call is no attempt and writes no row. Calls to a
local `fastembed/` embedding model skip it: it runs on our CPU (`LocalEmbeddingProvider`,
ADR-0018) and costs nothing, so a spent budget must not stop knowledge-base search or
indexing. Any other embedding model is checked like a chat call.
"""

import asyncio
import json
import logging
import os
import tempfile
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError
from uuid_utils.compat import uuid7

from hub.config import Settings
from hub.domain.enums import LlmCallContext, LlmCallMode, LlmCallStatus
from hub.guardrails.masking import mask
from hub.llm.budget import BudgetCheck
from hub.llm.config import ConfigError, ModelsConfig, RoleConfig, load_models
from hub.llm.local_embedding import PROVIDER_PREFIX as LOCAL_MODEL_PREFIX
from hub.llm.local_embedding import LocalEmbeddingProvider
from hub.llm.pricing import Pricing, load_pricing
from hub.llm.recorder import CallRecorder
from hub.llm.schemas import (
    BlockedReason,
    CallContext,
    Completion,
    Embeddings,
    FixtureMissingError,
    LlmCallRecord,
    LlmUnavailableError,
    Message,
    OutboundBlockedError,
    OutboundCardDataError,
    OutboundIbanError,
    OutputCheckError,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    Structured,
    StructuredOutputError,
    ToolCall,
    ToolsNotAllowedError,
    Usage,
    blocked_error,
    blocked_reason,
)
from hub.llm.structured import Invalid, check_answer, provider_schema, repair_messages
from hub.observability.tracing import Level, Observation, Tracer

# Switches LiteLLM reads from the environment (checked in LiteLLM 1.108.6). This is the one place
# in the Hub that sets environment variables (hub/config.py). They are set before `import litellm`
# and overwritten, not defaulted, so a stray value cannot turn the behaviour back on. Tests set
# them in tests/conftest.py by importing this module before anything else loads.
# - LITELLM_MODE: in its default "DEV" mode `litellm/__init__.py` calls `load_dotenv()`, which
#   copies the nearest `.env` (searched upward through parent folders) into `os.environ`. Settings
#   are read only by hub/config.py. Outside the proxy server, "PRODUCTION" changes nothing else.
# - LITELLM_LOCAL_MODEL_COST_MAP: otherwise LiteLLM fetches its price map from GitHub at import.
#   The Hub takes prices from config/pricing.yaml.
# - LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS: otherwise the first Anthropic call downloads, from
#   LiteLLM's main branch, the file that decides which `anthropic-beta` headers are sent
#   (`anthropic_beta_headers_manager.py`): unpinned remote input (threat T11).
LITELLM_SWITCHES = {
    "LITELLM_MODE": "PRODUCTION",
    "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    "LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS": "True",
}
os.environ.update(LITELLM_SWITCHES)
import litellm  # noqa: E402 - must come after the switches above
from litellm import exceptions as litellm_errors  # noqa: E402

# Request and response bodies must never reach LiteLLM's logging callbacks or debug output.
# LiteLLM's loggers propagate to the root logger with a DEBUG handler of their own; pinned at
# WARNING, a DEBUG log level of the app cannot print request bodies.
litellm.turn_off_message_logging = True
litellm.suppress_debug_info = True
LITELLM_LOGGERS = ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy")
for _name in LITELLM_LOGGERS:
    logging.getLogger(_name).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

RETRIES = 2
BACKOFF_S = 0.5  # waits 0.5 s, then 1 s

FIXTURES_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "llm"

# LiteLLM exceptions that are worth another attempt on the same model. Any status >= 500 is
# transient too: LiteLLM maps a 502 to `BadGatewayError`, which is none of these classes.
_TRANSIENT: tuple[type[Exception], ...] = (
    litellm_errors.Timeout,
    litellm_errors.RateLimitError,
    litellm_errors.APIConnectionError,
    litellm_errors.ServiceUnavailableError,
    litellm_errors.InternalServerError,
)


class Provider(Protocol):
    async def complete(self, request: ProviderRequest) -> ProviderResponse: ...

    async def embed(self, request: ProviderRequest) -> ProviderResponse: ...


# --- G2: outbound card scan ---------------------------------------------------------------------


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item)


def scan_outbound(request: ProviderRequest) -> dict[str, int]:
    """Card data (PAN, CVV, PIN, 2FA code) and raw IBANs anywhere in what the model would see:
    messages, texts, tool definitions, schema. G3 replaces IBANs before the model call, so a raw
    IBAN here is a mistake too. Each string is scanned on its own, so digits of two
    neighbouring strings never join into one number. Fails closed: a masking error counts."""
    seen = request.model_dump(include={"messages", "texts", "tools", "json_schema"})
    counts: Counter[str] = Counter()
    for text in _strings(seen):
        result = mask(text)
        if result.failed:
            counts["MASKING_FAILED"] += 1
        counts.update(result.counts)
    return {entity: count for entity, count in counts.items() if count}


def _blocked_error(reason: BlockedReason, message: str) -> OutboundBlockedError:
    if reason == "iban":
        return OutboundIbanError(f"{message}: raw IBAN")
    return OutboundCardDataError(f"{message}: card data")


# --- providers -----------------------------------------------------------------------------------


class LiteLLMProvider:
    """Real calls through the LiteLLM SDK. Keys come from the settings, never from the request
    or the environment, and LiteLLM's own retries are off: the gateway retries.

    `api_bases` exists for tests only (a local server that captures what LiteLLM sends); it is
    set in code, never from a config file or a request."""

    def __init__(self, settings: Settings, api_bases: Mapping[str, str] | None = None) -> None:
        self._keys = {"anthropic": settings.anthropic_api_key.get_secret_value()}
        self._api_bases = dict(api_bases or {})

    def _connection(self, model: str) -> dict[str, str]:
        provider = model.split("/", 1)[0]
        base = self._api_bases.get(provider)
        return {"api_key": self._key(model), **({"api_base": base} if base else {})}

    def _key(self, model: str) -> str:
        provider = model.split("/", 1)[0]
        key = self._keys.get(provider, "")
        if not key:
            raise ProviderError("MissingApiKey", transient=False)
        return key

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": request.messages,
            "max_tokens": request.max_tokens,
            "timeout": request.timeout_s,
            "num_retries": 0,
            "max_retries": 0,
            **self._connection(request.model),
            **request.params,
        }
        if request.json_schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "schema": request.json_schema,
                    "strict": True,
                },
            }
        if request.tools:
            kwargs["tools"] = request.tools
        try:
            response = await litellm.acompletion(**kwargs)
        except Exception as exc:
            raise _provider_error(exc) from None
        try:
            message = response.choices[0].message
            return ProviderResponse(
                content=message.content,
                tool_calls=[
                    ToolCall(id=call.id, name=call.function.name, arguments=call.function.arguments)
                    for call in (message.tool_calls or [])
                ],
                usage=_usage(response.usage),
            )
        except Exception:
            # An answer we cannot read (no choices, a tool call without id) is a failed call.
            raise ProviderError("InvalidResponse", transient=False) from None

    async def embed(self, request: ProviderRequest) -> ProviderResponse:
        """Embeddings come from a local model, never from a provider API (ADR-0018):
        `build_gateway` puts `LocalEmbeddingProvider` in front of this provider, so this is
        reached only when that wiring is missing. It fails without a retry; the gateway records
        it and raises `LlmUnavailableError`."""
        raise ProviderError("LocalEmbeddingNotAvailable", transient=False)


def _provider_error(exc: Exception) -> ProviderError:
    """Only the class and status code: provider messages can echo the request."""
    if isinstance(exc, ProviderError):
        return exc
    status_code = getattr(exc, "status_code", None)
    status = status_code if isinstance(status_code, int) else None
    return ProviderError(
        type(exc).__name__,
        transient=isinstance(exc, _TRANSIENT) or (status is not None and status >= 500),
        status_code=status,
    )


def _usage(usage: object) -> Usage:
    """LiteLLM's OpenAI-style usage. `prompt_tokens` includes cache reads and writes (LiteLLM
    adds Anthropic's `cache_read_input_tokens` and `cache_creation_input_tokens` to it)."""
    if usage is None:
        return Usage()
    details = getattr(usage, "prompt_tokens_details", None)
    return Usage(
        input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        cached_tokens=getattr(details, "cached_tokens", 0) or 0,
        cache_write_tokens=getattr(details, "cache_creation_tokens", 0) or 0,
    )


class Fixture(BaseModel):
    """A recorded response. `request` is kept for people reading the file; the key is the
    file name."""

    request: dict[str, Any]
    response: ProviderResponse
    recorded: str  # date of the real call, or "synthetic" for a hand-written response


class FixtureProvider:
    """Fake mode: answers from `<dir>/<fixture key>.json`. A missing file is an error, never a
    real call."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def path(self, request: ProviderRequest) -> Path:
        return self._directory / f"{request.fixture_key()}.json"

    def _load(self, request: ProviderRequest) -> ProviderResponse:
        path = self.path(request)
        try:
            return Fixture.model_validate_json(path.read_bytes()).response
        except FileNotFoundError:
            raise FixtureMissingError(
                f"no recorded response for {request.model} (key {request.fixture_key()}); "
                "add the request to tests/fixtures/llm/requests and run `make record-llm`"
            ) from None
        except ValidationError:
            raise FixtureMissingError(f"recorded response {path.name} is invalid") from None

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        return self._load(request)

    async def embed(self, request: ProviderRequest) -> ProviderResponse:
        return self._load(request)


class RecordingProvider:
    """Record mode (`make record-llm`): real calls, and every successful answer is written as a
    fixture. Used inside a `Gateway`, so recording gets the G2 scan, retries and `llm_calls` rows
    like any other call."""

    def __init__(self, inner: Provider, directory: Path) -> None:
        self._inner = inner
        self._directory = directory

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        response = await self._inner.complete(request)
        write_fixture(self._directory, request, response)
        return response

    async def embed(self, request: ProviderRequest) -> ProviderResponse:
        response = await self._inner.embed(request)
        write_fixture(self._directory, request, response)
        return response


class CachingProvider:
    """The evaluation disk cache (eval plan §4): `<dir>/<fixture key>.json`, in the fixture
    format. The key covers the model, its parameters and everything the model sees (the prompt
    text, so a new prompt version is a new key), so a prompt change re-runs only the calls whose
    input changed. A hit returns the stored answer marked `replayed` and calls nothing; a miss
    calls `inner`. The gateway calls `store` only for an answer that passed its check (P4-03
    review): a failed call or an answer the schema check rejects is never stored, so a passing
    model glitch is not replayed forever. A store that fails (disk full, permissions) is logged
    and ignored: the answer was paid and must reach the caller and its `llm_calls` row. `hits`
    and `misses` count the calls of this process, for the run summary.

    Callers keep one directory per `LLM_MODE`, so a fake answer is never replayed as a real
    one."""

    def __init__(self, inner: Provider, directory: Path) -> None:
        self._inner = inner
        self._directory = directory
        self.hits = 0
        self.misses = 0

    def path(self, request: ProviderRequest) -> Path:
        return self._directory / f"{request.fixture_key()}.json"

    def contains(self, request: ProviderRequest) -> bool:
        return self.path(request).is_file()

    def _load(self, request: ProviderRequest) -> ProviderResponse | None:
        try:
            stored = Fixture.model_validate_json(self.path(request).read_bytes())
        except FileNotFoundError:
            return None
        except (OSError, ValidationError) as exc:
            # An unreadable or broken cache file: a miss, never a failed call.
            logger.warning(
                "evaluation cache file is unreadable; calling the model again",
                extra={"error": type(exc).__name__},
            )
            return None
        return stored.response.model_copy(update={"replayed": True})

    async def _call(
        self,
        request: ProviderRequest,
        send: Callable[[ProviderRequest], Awaitable[ProviderResponse]],
    ) -> ProviderResponse:
        cached = self._load(request)
        if cached is not None:
            self.hits += 1
            return cached
        self.misses += 1
        return await send(request)

    def store(self, request: ProviderRequest, response: ProviderResponse) -> None:
        """Keep a checked answer. Never raises: a cache that cannot write must not fail the
        call. Only the error class is logged."""
        if response.replayed:
            return
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            write_fixture(self._directory, request, response)
        except (OSError, OutboundBlockedError) as exc:
            logger.warning(
                "evaluation cache could not store an answer", extra={"error": type(exc).__name__}
            )

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        return await self._call(request, self._inner.complete)

    async def embed(self, request: ProviderRequest) -> ProviderResponse:
        return await self._call(request, self._inner.embed)


def write_fixture(directory: Path, request: ProviderRequest, response: ProviderResponse) -> Path:
    """Used by `make record-llm`. The request is scanned again before it is written."""
    entities = scan_outbound(request)
    if entities:
        raise _blocked_error(blocked_reason(entities), "refusing to record the request")
    fixture = Fixture(
        request=request.model_dump(mode="json", exclude={"timeout_s"}),
        response=response,
        recorded=time.strftime("%Y-%m-%d"),
    )
    path = directory / f"{request.fixture_key()}.json"
    text = json.dumps(fixture.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
    # Written to a temporary file and moved into place, so an interrupted run (the evaluation
    # cache is written while tickets run in parallel) never leaves half a file behind.
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory, suffix=".tmp", delete=False
    ) as handle:
        handle.write(text)
    Path(handle.name).replace(path)
    return path


# --- the gateway ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Outcome:
    request: ProviderRequest
    response: ProviderResponse
    cost_usd: Decimal
    latency_ms: int
    index: int  # 0: the role's model answered; 1: its fallback
    invalid: Invalid | None = None  # set when `check` found the answer invalid

    @property
    def fallback_used(self) -> bool:
        return self.index > 0


type Check = Callable[[ProviderResponse], Invalid | None]


@dataclass(frozen=True)
class Gateway:
    models: ModelsConfig
    pricing: Pricing
    provider: Provider
    recorder: CallRecorder
    # Written to every row: only `real` rows count against the daily budget.
    mode: LlmCallMode
    # G8, checked before `online` calls only. None in unit tests and in record mode, whose
    # `record` calls are never refused (their rows still count against the day's budget).
    budget: BudgetCheck | None
    sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep)
    clock: Callable[[], float] = field(default=time.perf_counter)
    tracer: Tracer = field(default_factory=Tracer)

    async def complete(
        self,
        role: str,
        messages: Sequence[Message],
        schema: type[BaseModel] | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        *,
        call: CallContext,
    ) -> Completion:
        requests = self.chat_requests(role, messages, schema, tools)
        outcome = await self._run(requests, call, self.provider.complete)
        return Completion(
            content=outcome.response.content,
            tool_calls=outcome.response.tool_calls,
            model=outcome.request.model,
            usage=outcome.response.usage,
            cost_usd=outcome.cost_usd,
            latency_ms=outcome.latency_ms,
            fallback_used=outcome.fallback_used,
        )

    async def complete_structured[T: BaseModel](
        self,
        role: str,
        messages: Sequence[Message],
        schema: type[T],
        *,
        call: CallContext,
    ) -> Structured[T]:
        """A validated `schema` instance (ADR-0004). An invalid answer gets one repair call with
        the validation errors, sent to the model that wrote the invalid answer (then the usual
        retries, and the fallback when that was the role's model). Raises
        `StructuredOutputError` when the repair answer is invalid too, and the usual gateway
        errors when a call fails."""

        def check(response: ProviderResponse) -> Invalid | None:
            result = check_answer(schema, response.content)
            return result if isinstance(result, Invalid) else None

        requests = self.chat_requests(role, messages, schema)
        first = await self._run(requests, call, self.provider.complete, check)
        if first.invalid is None:
            return _structured(schema, first, [first])
        repair = self.chat_requests(
            role, repair_messages(messages, first.response.content, first.invalid), schema
        )
        second = await self._run(
            repair[first.index :], call, self.provider.complete, check, first_index=first.index
        )
        if second.invalid is not None:
            raise StructuredOutputError(schema.__name__, len(second.invalid.errors))
        return _structured(schema, second, [first, second])

    async def embed(
        self, texts: Sequence[str], *, call: CallContext, role: str = "embedding"
    ) -> Embeddings:
        request = self.embedding_request(texts, role)
        # Only the local model is free (ADR-0018); any other embedding model is guarded.
        outcome = await self._run(
            [request],
            call,
            self.provider.embed,
            budget_check=not request.model.startswith(LOCAL_MODEL_PREFIX),
        )
        if len(outcome.response.vectors) != len(texts):
            raise LlmUnavailableError(f"role {role}: wrong number of vectors")
        return Embeddings(
            vectors=outcome.response.vectors,
            model=request.model,
            usage=outcome.response.usage,
            cost_usd=outcome.cost_usd,
            latency_ms=outcome.latency_ms,
        )

    async def record_each_model(
        self,
        role: str,
        *,
        messages: Sequence[Message] = (),
        texts: Sequence[str] = (),
        tools: Sequence[dict[str, Any]] | None = None,
        schema: type[BaseModel] | None = None,
        call: CallContext,
        select: Callable[[ProviderRequest], bool] | None = None,
    ) -> list[ProviderRequest]:
        """Record mode: call the role's model and its fallback each once (with the usual G2 scan,
        retries and rows), so fake mode has an answer for both. `schema` builds the request as
        `complete_structured` does (the answer is recorded as it came, not validated: a test
        that reads the fixture shows a bad answer). `select` keeps only some of the requests,
        e.g. the role's own model, or those without a real recording yet. Returns the requests
        sent."""
        if texts:
            requests = [self.embedding_request(texts, role)]
            send = self.provider.embed
        else:
            requests = self.chat_requests(role, messages, schema, tools)
            send = self.provider.complete
        chosen = [request for request in requests if select is None or select(request)]
        for request in chosen:
            await self._run([request], call, send)
        return chosen

    def chat_requests(
        self,
        role: str,
        messages: Sequence[Message],
        schema: type[BaseModel] | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> list[ProviderRequest]:
        """The request to the role's model, then to its fallback. `make record-llm` uses the
        same function, so recorded fixtures match what `complete` sends."""
        config = self._role(role, "chat")
        if tools and not config.tools:
            raise ToolsNotAllowedError(f"role {role} is not configured for tools")
        return [
            ProviderRequest(
                kind="chat",
                model=choice.model,
                messages=[dict(message) for message in messages],
                max_tokens=config.max_tokens,
                timeout_s=config.timeout_s,
                params=choice.params,
                schema_name=schema.__name__ if schema is not None else None,
                json_schema=provider_schema(schema) if schema is not None else None,
                tools=[dict(tool) for tool in tools] if tools else None,
            )
            for choice in config.choices()
        ]

    def embedding_request(self, texts: Sequence[str], role: str = "embedding") -> ProviderRequest:
        config = self._role(role, "embedding")
        return ProviderRequest(
            kind="embedding", model=config.model, texts=list(texts), timeout_s=config.timeout_s
        )

    def _role(self, role: str, kind: str) -> RoleConfig:
        config = self.models.role(role)
        if config.kind != kind:
            raise ConfigError(f"role {role} is not a {kind} role")
        return config

    async def _run(
        self,
        requests: list[ProviderRequest],
        call: CallContext,
        send: Callable[[ProviderRequest], Awaitable[ProviderResponse]],
        check: Check | None = None,
        *,
        first_index: int = 0,
        budget_check: bool = True,
    ) -> _Outcome:
        """`first_index`: position of `requests[0]` in the role's model list (1 when the call
        starts at the fallback), so `_Outcome.index` always names the model's role.
        `budget_check`: false only for a local `fastembed/` embedding model (module docstring)."""
        if budget_check and call.context == LlmCallContext.ONLINE and self.budget is not None:
            await self.budget.check()
        # The model and fallback requests differ only in model and parameters: one scan covers
        # what every model would see.
        entities = scan_outbound(requests[0])
        if entities:
            reason = blocked_reason(entities)
            self.tracer.start(
                "G2", as_type="guardrail", metadata={"node": call.node.value, "entities": entities}
            ).end(level="ERROR", status_message=reason)
            await self.recorder.record_blocked(
                self._row(
                    call, requests[0].model, LlmCallStatus.ERROR, error=blocked_error(reason)
                ),
                entities,
            )
            raise _blocked_error(reason, f"G2 blocked a call of node {call.node.value}")

        for index, request in enumerate(requests, start=first_index):
            for attempt in range(RETRIES + 1):
                observation = self._observe(request, call, index, attempt)
                start = self.clock()
                try:
                    async with asyncio.timeout(request.timeout_s):
                        response = await send(request)
                except TimeoutError:
                    error = ProviderError("Timeout", transient=True)
                except ProviderError as exc:
                    error = exc
                except FixtureMissingError:
                    # Fake mode without a recording: a test or setup mistake. Recorded, then
                    # raised as it is, never sent to the fallback.
                    await self._record(
                        observation,
                        self._row(
                            call,
                            request.model,
                            LlmCallStatus.ERROR,
                            latency_ms=self._elapsed_ms(start),
                            error="FixtureMissing",
                        ),
                    )
                    raise
                except Exception as exc:
                    # Any other failure of the provider: recorded, not retried, fallback next.
                    error = ProviderError(type(exc).__name__, transient=False)
                else:
                    latency_ms = self._elapsed_ms(start)
                    try:
                        cost = self.pricing.cost(request.model, response.usage)
                    except ConfigError:
                        # The call happened and was paid: keep its tokens, then stop.
                        await self._record(
                            observation,
                            self._row(
                                call,
                                request.model,
                                LlmCallStatus.ERROR,
                                usage=response.usage,
                                latency_ms=latency_ms,
                                error="PricingError",
                            ),
                        )
                        raise
                    # A paid answer that fails the schema is not a provider failure: no retry and
                    # no fallback here; complete_structured decides about the repair call.
                    try:
                        invalid = check(response) if check is not None else None
                    except Exception as exc:
                        # The call was paid: keep its row, then stop. Only the class is kept;
                        # the message may quote the answer.
                        kind = type(exc).__name__
                        await self._record(
                            observation,
                            self._row(
                                call,
                                request.model,
                                LlmCallStatus.ERROR,
                                usage=response.usage,
                                cost=cost,
                                latency_ms=latency_ms,
                                error=f"CheckFailed ({kind})",
                            ),
                        )
                        raise OutputCheckError(
                            f"node {call.node.value}: the answer check failed ({kind})"
                        ) from None
                    if invalid is None and isinstance(self.provider, CachingProvider):
                        self.provider.store(request, response)
                    await self._record(
                        observation,
                        self._row(
                            call,
                            request.model,
                            LlmCallStatus.OK if invalid is None else LlmCallStatus.INVALID_OUTPUT,
                            usage=response.usage,
                            cost=cost,
                            latency_ms=latency_ms,
                            error=invalid.summary if invalid is not None else None,
                            replayed=response.replayed,
                        ),
                        response,
                    )
                    return _Outcome(request, response, cost, latency_ms, index, invalid)
                will_retry = error.transient and attempt < RETRIES
                await self._record(
                    observation,
                    self._row(
                        call,
                        request.model,
                        LlmCallStatus.RETRY if will_retry else LlmCallStatus.ERROR,
                        latency_ms=self._elapsed_ms(start),
                        error=error.summary,
                    ),
                )
                if not will_retry:
                    break
                await self.sleep(BACKOFF_S * 2**attempt)
        raise LlmUnavailableError(f"node {call.node.value}: the model and its fallback failed")

    async def record_abandoned(
        self, role: str, call: CallContext, *, latency_ms: int, error: str = "CallerTimeout"
    ) -> None:
        """One `error` row for a call its caller stopped waiting for (an outer timeout cancels
        the attempt before the gateway can record it). Named after the role's model: the caller
        cannot know whether the fallback was reached."""
        config = self._role(role, "chat")
        await self.recorder.record(
            self._row(call, config.model, LlmCallStatus.ERROR, latency_ms=latency_ms, error=error)
        )

    def _observe(
        self, request: ProviderRequest, call: CallContext, index: int, attempt: int
    ) -> Observation:
        """The attempt's generation. Chat messages are already masked (G1, then the G2 scan);
        an embedding's texts are not sent, only their number."""
        return self.tracer.start(
            call.node.value,
            as_type="generation" if request.kind == "chat" else "embedding",
            model=request.model,
            version=call.prompt_version,
            input=request.messages if request.kind == "chat" else {"texts": len(request.texts)},
            model_parameters={"max_tokens": request.max_tokens} if request.max_tokens else None,
            metadata={
                "prompt_version": call.prompt_version,
                "context": call.context.value,
                "attempt": attempt + 1,
                "fallback": index > 0,
                "schema": request.schema_name,
                "tools": [tool.get("name") for tool in request.tools or []] or None,
            },
        )

    async def _record(
        self,
        observation: Observation,
        row: LlmCallRecord,
        response: ProviderResponse | None = None,
    ) -> None:
        """The attempt's `llm_calls` row, then its generation closed with the same numbers."""
        await self.recorder.record(row)
        observation.end(
            output=_trace_output(response),
            usage_details={
                "input": row.input_tokens,
                "output": row.output_tokens,
                "cache_read_input_tokens": row.cached_tokens,
            },
            cost_details={"total": float(row.cost_usd)},
            level=_TRACE_LEVELS[row.status],
            status_message=row.error,
            metadata={"status": row.status.value, "latency_ms": row.latency_ms},
        )

    def _elapsed_ms(self, start: float) -> int:
        return max(0, round((self.clock() - start) * 1000))

    def _row(
        self,
        call: CallContext,
        model: str,
        status: LlmCallStatus,
        *,
        usage: Usage | None = None,
        cost: Decimal = Decimal(0),
        latency_ms: int = 0,
        error: str | None = None,
        replayed: bool = False,
    ) -> LlmCallRecord:
        """`replayed`: an answer from the evaluation cache, recorded as `fake` (nothing paid)."""
        usage = usage or Usage()
        return LlmCallRecord(
            id=uuid7(),
            run_id=call.run_id,
            context=call.context,
            mode=LlmCallMode.FAKE if replayed else self.mode,
            node=call.node,
            model=model,
            prompt_version=call.prompt_version,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_tokens=usage.cached_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
            status=status,
            error=error,
        )


_TRACE_LEVELS: dict[LlmCallStatus, Level] = {
    LlmCallStatus.OK: "DEFAULT",
    LlmCallStatus.RETRY: "WARNING",
    LlmCallStatus.INVALID_OUTPUT: "WARNING",
    LlmCallStatus.ERROR: "ERROR",
}


def _trace_output(response: ProviderResponse | None) -> object:
    """What a generation shows as its output: the answer (masked by the tracer), the tool calls
    the model asked for, or only the number of vectors."""
    if response is None:
        return None
    if response.vectors:
        return {"vectors": len(response.vectors)}
    if response.tool_calls:
        return {
            "content": response.content,
            "tool_calls": [call.model_dump() for call in response.tool_calls],
        }
    return response.content


def _structured[T: BaseModel](
    schema: type[T], last: _Outcome, outcomes: Sequence[_Outcome]
) -> Structured[T]:
    usages = [outcome.response.usage for outcome in outcomes]
    return Structured(
        value=schema.model_validate_json(last.response.content or ""),
        model=last.request.model,
        usage=Usage(
            input_tokens=sum(usage.input_tokens for usage in usages),
            output_tokens=sum(usage.output_tokens for usage in usages),
            cached_tokens=sum(usage.cached_tokens for usage in usages),
            cache_write_tokens=sum(usage.cache_write_tokens for usage in usages),
        ),
        cost_usd=sum((outcome.cost_usd for outcome in outcomes), Decimal(0)),
        latency_ms=sum(outcome.latency_ms for outcome in outcomes),
        fallback_used=any(outcome.fallback_used for outcome in outcomes),
        repaired=len(outcomes) > 1,
    )


def build_gateway(
    settings: Settings,
    recorder: CallRecorder,
    budget: BudgetCheck | None,
    *,
    cache_dir: Path | None = None,
    tracer: Tracer | None = None,
) -> Gateway:
    """The gateway of the app: config files next to each other, provider and row mode from
    LLM_MODE. Embeddings always come from the local model (`LocalEmbeddingProvider`, in every
    mode: it is free and needs no network). `budget`: the app always passes its guard.
    `cache_dir`: evaluation runs only, the disk cache (`CachingProvider`) in
    `<cache_dir>/<LLM_MODE>/`. `tracer`: the process's tracer (off when None)."""
    models = load_models(settings.models_config)
    pricing = load_pricing(settings.models_config.parent / "pricing.yaml")
    pricing.check_covers(models)
    chat: Provider = (
        LiteLLMProvider(settings) if settings.llm_mode == "real" else FixtureProvider(FIXTURES_DIR)
    )
    provider: Provider = LocalEmbeddingProvider(chat, settings.embedding_cache_dir)
    if cache_dir is not None:
        provider = CachingProvider(provider, cache_dir / settings.llm_mode)
    return Gateway(
        models=models,
        pricing=pricing,
        provider=provider,
        recorder=recorder,
        mode=LlmCallMode(settings.llm_mode),
        budget=budget,
        tracer=tracer or Tracer(),
    )


def embedder(
    gateway: Gateway, call: CallContext
) -> Callable[[Sequence[str]], Awaitable[list[list[float]]]]:
    """Dense vectors through the gateway for one call context, in the shape `hub.kb` asks for
    (`hub.kb.index.DenseEmbedder`), so the knowledge base needs no gateway types."""

    async def embed(texts: Sequence[str]) -> list[list[float]]:
        return (await gateway.embed(texts, call=call)).vectors

    return embed
