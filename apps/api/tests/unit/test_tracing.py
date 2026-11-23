"""Run traces (P9-01, `hub.observability.tracing`): the G9 filter, tracing off without keys,
sampling of evaluation runs, and one traced run exported to memory (no network)."""

import dataclasses
import json
import uuid
from collections.abc import Iterator
from decimal import Decimal
from enum import Enum
from typing import Any, cast

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel

from hub.config import Settings
from hub.domain.enums import LlmCallStatus, ToolNode
from hub.llm.schemas import ProviderError
from hub.observability import tracing
from hub.observability.tracing import Observation, Tracer, sampled, trace_safe, trace_url
from hub.tools.mcp_client import ToolClient, TransportResult
from hub.tools.permissions import Caller, ToolScope

from ._llm import CALL, MemoryRecorder, ScriptedProvider, make_gateway, ok, real_models
from .test_tool_client import MemoryRecorder as ToolRecorder

VISA = "4111 1111 1111 1111"  # public test card number
VISA_DIGITS = "4111111111111111"
RUN_ID = uuid.UUID("0193a0c2-7d5e-7a01-8b02-00000000c001")
TICKET_ID = uuid.UUID("0193a0c2-7d5e-7a01-8b02-00000000b002")  # the tool tests' ticket
REASON = "sanctions list match"  # stands for a restriction reason; must never be exported


def _settings(**changes: Any) -> Settings:  # noqa: ANN401 - settings fields
    # A closed local port: if anything tried the network, it would fail here, not reach a host.
    fields: dict[str, Any] = {
        "_env_file": None,
        "app_env": "test",
        "langfuse_public_key": f"pk-lf-test-{uuid.uuid4()}",
        "langfuse_secret_key": "sk-lf-test",
        "langfuse_host": "http://127.0.0.1:9",
    }
    return Settings(**(fields | changes))


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def tracer(exporter: InMemorySpanExporter) -> Iterator[Tracer]:
    # A new public key per test: Langfuse keeps one client per key.
    on = Tracer.from_settings(_settings(eval_trace_sample=0.0), span_exporter=exporter)
    yield on
    on.flush()


def _spans(tracer: Tracer, exporter: InMemorySpanExporter) -> list[ReadableSpan]:
    tracer.flush()
    return list(exporter.get_finished_spans())


def _attributes(span: ReadableSpan) -> dict[str, Any]:
    return dict(span.attributes or {})


def _everything(spans: list[ReadableSpan]) -> str:
    return json.dumps([_attributes(span) for span in spans], default=str)


def _run(tracer: Tracer, context: str = "online", run_id: uuid.UUID = RUN_ID) -> Any:  # noqa: ANN401
    return tracer.run(
        run_id,
        ticket_id=TICKET_ID,
        context=context,
        name="run.start",
        attributes={"run_id": str(run_id), "graph_version": "graph-v4"},
    )


# --- G9: what may go into a trace ---------------------------------------------------------------


class _Nested(BaseModel):
    note: str
    restriction_reason: str | None = None


def test_trace_safe_masks_card_data_and_drops_restriction_reasons() -> None:
    value = {
        "text": f"my card {VISA} cvv 123",
        "count": int(VISA_DIGITS),
        "amount": Decimal("12.50"),
        "restriction_reason": REASON,
        "Restriction Reason": REASON,
        "customer": {"restrictionReason": REASON, "restricted": True},
        "items": [f"card {VISA}", _Nested(note="ok", restriction_reason=REASON)],
        "status": LlmCallStatus.OK,
        "id": RUN_ID,
        "nothing": None,
    }
    safe = trace_safe(value)
    text = json.dumps(safe)
    assert VISA not in text
    assert VISA_DIGITS not in text
    assert "123" not in text
    assert REASON not in text
    assert "[CARD ****1111]" in safe["text"]
    assert safe["customer"] == {"restricted": True}
    assert safe["items"][1] == {"note": "ok"}
    assert safe["amount"] == 12.5
    assert safe["status"] == "ok"
    # Ids stay readable, so a trace can be searched by run or ticket id (P8-03).
    assert safe["id"] == str(RUN_ID)
    assert safe["nothing"] is None


def test_the_langfuse_mask_hook_is_the_same_filter() -> None:
    assert tracing._mask_hook(data={"restriction_reason": REASON, "t": VISA}) == {
        "t": "[CARD ****1111]"
    }


# --- off, sampling, links -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [{}, {"langfuse_public_key": "pk"}, {"langfuse_secret_key": "sk"}],
)
def test_tracing_is_off_while_a_key_is_missing(changes: dict[str, str]) -> None:
    off = Tracer.from_settings(
        Settings(_env_file=None, app_env="test", **changes)  # type: ignore[arg-type]
    )
    assert not off.enabled
    assert off.trace_id(RUN_ID, "online") is None
    with _run(off) as root, off.observe("write") as span:
        assert not root.recording
        assert not span.recording
        span.update(output=VISA)
    off.start("triage", as_type="generation").end(output=VISA)
    off.flush()


def test_every_online_run_is_traced_and_eval_runs_by_a_stable_share() -> None:
    ids = [uuid.UUID(int=n) for n in range(2000)]
    assert all(sampled(run_id, "online", 0.0) for run_id in ids)
    assert not any(sampled(run_id, "eval", 0.0) for run_id in ids)
    assert all(sampled(run_id, "eval", 1.0) for run_id in ids)
    share = sum(sampled(run_id, "eval", 0.2) for run_id in ids) / len(ids)
    assert 0.17 < share < 0.23
    # The same run makes the same choice: its resume joins the same trace or none.
    assert [sampled(run_id, "eval", 0.2) for run_id in ids] == [
        sampled(run_id, "eval", 0.2) for run_id in ids
    ]


def test_the_trace_id_comes_from_the_run_id(tracer: Tracer) -> None:
    first = tracer.trace_id(RUN_ID, "online")
    assert first is not None
    assert len(first) == 32
    assert first == tracer.trace_id(RUN_ID, "online")
    assert first != tracer.trace_id(uuid.uuid4(), "online")
    assert tracer.trace_id(RUN_ID, "eval") is None  # the fixture samples no eval run


def test_the_trace_link_needs_a_trace_and_a_project() -> None:
    assert trace_url("", "p1", "abc") == "https://cloud.langfuse.com/project/p1/traces/abc"
    assert (
        trace_url("https://lf.example/", "p1", "abc") == "https://lf.example/project/p1/traces/abc"
    )
    assert trace_url("", "", "abc") is None
    assert trace_url("", "p1", None) is None


# --- one traced run, exported to memory ---------------------------------------------------------


class _Core:
    """A tool server whose answer carries what must never reach a trace."""

    async def call(self, name: str, arguments: dict[str, Any], timeout_s: float) -> TransportResult:
        return TransportResult(
            False,
            {
                "customer_ref": arguments.get("customer_ref"),
                "restriction_reason": REASON,
                "note": f"card {VISA}",
            },
        )


async def test_a_run_is_one_trace_with_nodes_model_and_tool_calls(
    tracer: Tracer, exporter: InMemorySpanExporter
) -> None:
    provider = ScriptedProvider(
        {
            real_models().role("triage").model: [
                ProviderError("Timeout", transient=True),
                ok(f"the card {VISA} is fine"),
            ]
        }
    )
    gateway = dataclasses.replace(make_gateway(provider, MemoryRecorder()), tracer=tracer)
    tools = ToolClient(_Core(), ToolRecorder(), timeout_s=1.0, tracer=tracer)
    scope = ToolScope(
        run_id=RUN_ID, ticket_id=TICKET_ID, customer_ref="CUS-0001", node=ToolNode.RESEARCH
    )
    with _run(tracer) as root:
        assert root.recording
        with tracer.observe("research"):
            await gateway.complete("triage", [{"role": "user", "content": "hello"}], call=CALL)
            await tools.session(scope).call("get_account", caller=Caller.MODEL)
            await tools.session(scope).call("jira_create_issue", {"summary": VISA})
        root.update(output={"route": "approval"})

    spans = _spans(tracer, exporter)
    by_name: dict[str, list[ReadableSpan]] = {}
    for span in spans:
        by_name.setdefault(span.name, []).append(span)
    trace_id = int(tracer.trace_id(RUN_ID, "online") or "0", 16)
    assert {span.context.trace_id for span in spans if span.context} == {trace_id}

    (node,) = by_name["research"]
    retry, answer = sorted(by_name["triage"], key=lambda span: span.start_time or 0)
    for generation in (retry, answer):
        assert generation.parent is not None
        assert node.context is not None
        assert generation.parent.span_id == node.context.span_id
        attributes = _attributes(generation)
        assert attributes["langfuse.observation.type"] == "generation"
        assert attributes["langfuse.observation.model.name"] == real_models().role("triage").model
        assert json.loads(attributes["langfuse.observation.metadata.prompt_version"]) == "v1"
    assert _attributes(retry)["langfuse.observation.level"] == "WARNING"
    done = _attributes(answer)
    assert json.loads(done["langfuse.observation.usage_details"])["input"] == 100
    assert json.loads(done["langfuse.observation.cost_details"])["total"] > 0
    assert "[CARD ****1111]" in done["langfuse.observation.output"]

    (profile,) = by_name["core_get_customer"]
    assert _attributes(profile)["langfuse.observation.type"] == "tool"
    (denied,) = by_name["jira_create_issue"]
    assert _attributes(denied)["langfuse.observation.level"] == "WARNING"

    exported = _everything(spans)
    assert VISA not in exported
    assert VISA_DIGITS not in exported
    assert REASON not in exported
    assert "restriction_reason" not in exported


async def test_nothing_is_traced_outside_a_run_or_for_an_unsampled_eval_run(
    tracer: Tracer, exporter: InMemorySpanExporter
) -> None:
    gateway = dataclasses.replace(
        make_gateway(ScriptedProvider({real_models().role("triage").model: [ok(), ok()]})),
        tracer=tracer,
    )
    await gateway.complete("triage", [{"role": "user", "content": "hi"}], call=CALL)
    assert not tracer.start("x", as_type="span").recording
    with _run(tracer, context="eval") as root, tracer.observe("write") as span:
        assert not root.recording
        assert not span.recording
        await gateway.complete("triage", [{"role": "user", "content": "hi"}], call=CALL)
    assert _spans(tracer, exporter) == []


async def test_a_blocked_call_is_a_guardrail_observation_without_the_text(
    tracer: Tracer, exporter: InMemorySpanExporter
) -> None:
    gateway = dataclasses.replace(make_gateway(ScriptedProvider({})), tracer=tracer)
    with _run(tracer), pytest.raises(Exception, match="G2"):
        await gateway.complete("triage", [{"role": "user", "content": f"card {VISA}"}], call=CALL)
    spans = _spans(tracer, exporter)
    (guardrail,) = [span for span in spans if span.name == "G2"]
    assert _attributes(guardrail)["langfuse.observation.level"] == "ERROR"
    assert VISA_DIGITS not in _everything(spans)


class _Colour(Enum):
    RED = "card 4111 1111 1111 1111"


def test_a_plain_enum_is_filtered_by_its_value() -> None:
    assert trace_safe(_Colour.RED) == "card [CARD ****1111]"
    assert trace_safe({1, 2}) in ([1, 2], [2, 1])
    assert trace_safe(object()).startswith("<object object")


class _FailingExit:
    def __enter__(self) -> "_FailingExit":
        return self

    def __exit__(self, *_args: object) -> None:
        raise RuntimeError("export down")

    def update(self, **_fields: Any) -> None:  # noqa: ANN401
        return None


class _BrokenClient:
    """A Langfuse client whose every call fails, or whose spans fail to close."""

    def __init__(self, *, fail_start: bool) -> None:
        self.fail_start = fail_start

    def start_as_current_observation(self, **_fields: Any) -> _FailingExit:  # noqa: ANN401
        if self.fail_start:
            raise RuntimeError("sdk down")
        return _FailingExit()

    def start_observation(self, **_fields: Any) -> None:  # noqa: ANN401
        raise RuntimeError("sdk down")

    def flush(self) -> None:
        raise RuntimeError("sdk down")


@pytest.mark.parametrize("fail_start", [True, False])
def test_a_failing_sdk_never_breaks_a_run(fail_start: bool) -> None:
    broken = Tracer(client=cast(Any, _BrokenClient(fail_start=fail_start)))
    ran: list[str] = []
    with _run(broken) as root:
        assert root.recording is not fail_start
        with broken.observe("write") as span:
            assert span.recording is not fail_start
            ran.append("node")
        assert not broken.start("triage", as_type="generation").recording
    broken.flush()
    assert ran == ["node"]


def test_a_failing_sdk_never_breaks_the_caller() -> None:
    class Broken:
        def update(self, **_fields: Any) -> None:  # noqa: ANN401
            raise RuntimeError("sdk down")

        def end(self) -> None:
            raise RuntimeError("sdk down")

    observation = Observation(Broken())
    observation.update(output="x")
    observation.end(output="x")
