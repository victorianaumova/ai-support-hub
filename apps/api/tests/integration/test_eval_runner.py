"""The evaluation runner against Postgres (P4-03) and Qdrant (P5-05).

- `make migrate` also upgrades the evaluation database (`-x db=eval`), after which the app role
  can write there (default privileges are per database).
- The runner sends evaluation tickets through the production `run_ticket`, resumes an
  interrupted run without repeating finished tickets, serves a re-run from the disk cache with no
  provider call, and stops before the first call when the estimate is above the limit.

Model answers come from the recordings in `tests/fixtures/llm` (the run tickets of
`tests/fixtures/run/tickets.jsonl`), served through a counting provider so the test sees every
call that would have reached a model. These tests run baseline B0; the RAG variant has its own
test at the end, with a scripted writer and verifier, the real local models and Qdrant.
"""

import json
import os
import re
import subprocess
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from hub.config import Settings
from hub.db.repositories import CustomerRepository, TicketRepository
from hub.db.session import connect
from hub.domain.enums import Intent
from hub.domain.enums import TicketStatus as S
from hub.domain.memory import shows_open_case
from hub.graph.nodes.intake import customer_context
from hub.graph.pipeline import B0_VARIANT
from hub.llm.gateway import FIXTURES_DIR, FixtureProvider
from hub.llm.schemas import ProviderError, ProviderRequest, ProviderResponse, Usage
from hub.observability.readiness import libpq_url
from hub_evals import results as files
from hub_evals.runner import (
    ESTIMATED_CITATIONS,
    TICKETS_FILE,
    EvalSetupError,
    Options,
    close_open_tickets,
    load_tickets,
    run,
)

from ._api import Urls, own_database, run_sql
from ._compose import ROOT
from ._db import ALEMBIC_INI, app_url, as_user
from ._kb import MODEL_DIR

DATABASE = "hub_eval_runner_test"
MIGRATE_DATABASE = "hub_eval_migrate_test"
RUN_TICKETS = {
    json.loads(line)["id"]: json.loads(line)["ticket"]
    for line in (ROOT / "apps/api/tests/fixtures/run/tickets.jsonl").read_text().splitlines()
    if line.strip()
}
NOT_RECORDED = "This exact text has no recorded model answer, so the run must fail."


# --- migrate step -------------------------------------------------------------------------------


def test_make_migrate_upgrades_the_eval_database_for_the_app_role(
    postgres_url: str, tmp_path: Path
) -> None:
    with psycopg.connect(libpq_url(postgres_url), autocommit=True) as connection:
        connection.execute(
            sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(MIGRATE_DATABASE))
        )
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(MIGRATE_DATABASE)))
    try:
        # The command of `make migrate`, with the migrator URL derived from DATABASE_URL_DIRECT
        # (EVAL_DATABASE_URL_DIRECT empty) and no .env (cwd is an empty folder).
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("DATABASE_URL", "EVAL_DATABASE_URL"))
        }
        env["DATABASE_URL_DIRECT"] = as_user(postgres_url, "hub", "hub")
        env["EVAL_DATABASE_URL"] = app_url(postgres_url, database=MIGRATE_DATABASE)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "alembic",
                "-c",
                str(ALEMBIC_INI),
                "-x",
                "db=eval",
                "upgrade",
                "head",
            ],
            cwd=tmp_path,
            env=env,
            check=True,
            capture_output=True,
        )
        urls = (
            as_user(postgres_url, "hub", "hub", database=MIGRATE_DATABASE),
            app_url(postgres_url, database=MIGRATE_DATABASE),
        )
        # The app role writes there now.
        with psycopg.connect(libpq_url(urls[1]), autocommit=True) as connection:
            connection.execute(
                "INSERT INTO customers (id, external_ref, display_name, email, tier, country)"
                " VALUES (gen_random_uuid(), 'CUS-0001', 'A', 'a@example.com', 'verified', 'SK')"
            )
        assert run_sql(urls, "SELECT count(*) FROM customers") == [(1,)]
        wrong = _alembic(tmp_path, env, "db=other")
        assert wrong.returncode != 0
        assert "unknown -x db=other" in wrong.stderr
        # An older .env without an evaluation database: skipped, `make migrate` still passes.
        skipped = _alembic(tmp_path, env | {"EVAL_DATABASE_URL": ""}, "db=eval")
        assert skipped.returncode == 0
        assert "evaluation database skipped" in skipped.stdout
        # The evaluation database must never be the app database.
        same = _alembic(
            tmp_path,
            env
            | {
                "EVAL_DATABASE_URL": app_url(postgres_url),
                "DATABASE_URL_DIRECT": env["DATABASE_URL_DIRECT"],
            },
            "db=eval",
        )
        assert same.returncode != 0
        assert "is the app database" in same.stderr
    finally:
        with psycopg.connect(libpq_url(postgres_url), autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(MIGRATE_DATABASE)
                )
            )


def _alembic(cwd: Path, env: dict[str, str], target: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), "-x", target, "upgrade", "head"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


# --- the runner ---------------------------------------------------------------------------------


@dataclass
class CountingProvider:
    """The recordings, counting every call that would have reached a model."""

    inner: FixtureProvider = field(default_factory=lambda: FixtureProvider(FIXTURES_DIR))
    calls: int = 0

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.calls += 1
        return await self.inner.complete(request)

    async def embed(self, request: ProviderRequest) -> ProviderResponse:
        self.calls += 1
        return await self.inner.embed(request)


@pytest.fixture(scope="module")
def eval_db(postgres_url: str) -> Iterator[Urls]:
    with own_database(postgres_url, DATABASE) as urls:
        yield urls


def _tickets_file(path: Path, texts: list[str]) -> Path:
    base = load_tickets(TICKETS_FILE)[1]
    assert base.history == []
    lines = [
        base.model_copy(update={"id": f"T-90{number:02d}", "message": text}).model_dump_json()
        for number, text in enumerate(texts, start=1)
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _settings(urls: Urls, **changes: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "test",
        # Any database but the evaluation one: the runner never touches it.
        "database_url": "postgresql+psycopg://hub_api:hub_api@localhost:1/hub",
        "eval_database_url": urls[1],
        "llm_mode": "real",
        "models_config": ROOT / "config" / "models.yaml",
        "prompts_dir": ROOT / "prompts",
    }
    return Settings(_env_file=None, **(values | changes))  # type: ignore[arg-type]


def _results(run_dir: Path) -> dict[str, files.TicketResult]:
    return {r.ticket_id: r for r in files.read_results(run_dir / files.RESULTS_FILE)}


async def test_resume_cache_and_results(eval_db: Urls, tmp_path: Path) -> None:
    tickets = _tickets_file(
        tmp_path / "tickets.jsonl", [RUN_TICKETS["run_01"], RUN_TICKETS["run_02"], NOT_RECORDED]
    )
    provider = CountingProvider()
    settings = _settings(eval_db)
    run_dir = tmp_path / "run"
    # Judges off: this test counts the calls of the answers (judges: the test below).
    options = Options(
        tools=False,
        tickets=tickets,
        variant=B0_VARIANT,
        limit=2,
        run_dir=run_dir,
        cache_dir=tmp_path / "cache",
        runs_dir=tmp_path,
        judges=False,
    )

    first = await run(settings, options, provider=provider)
    assert (first.planned, first.skipped, first.ran) == (2, 0, 2)
    assert provider.calls == 4  # triage and write of two tickets
    assert first.estimate is not None
    assert first.estimate.calls == 4
    assert first.estimate.cached_calls == 0
    done = _results(run_dir)
    sepa, no_answer = done["T-9001"], done["T-9002"]
    assert (sepa.route, sepa.route_reasons, sepa.ticket_status) == (
        "approval",
        ["auto_reply_off"],
        "pending_approval",
    )
    assert sepa.draft is not None
    assert sepa.draft.can_answer
    assert sepa.draft.evidence["variant"] == "b0_full_kb"
    assert sepa.triage.intent is not None
    assert sepa.paid_cost_usd == sepa.cost_usd > 0
    assert sepa.llm_calls == 2
    assert sepa.events == ["run.status", "run.status", "ticket.status"]
    assert no_answer.draft is not None
    assert not no_answer.draft.can_answer
    rows = run_sql(eval_db, "SELECT DISTINCT context, mode FROM llm_calls")
    assert rows == [("eval", "real")]

    # An interrupted write, then a resume with all three tickets: only the third runs.
    with (run_dir / files.RESULTS_FILE).open("a", encoding="utf-8") as handle:
        handle.write('{"ticket_id": "T-9003", "run_')
    second = await run(settings, dataclass_replace(options, limit=None), provider=provider)
    assert (second.planned, second.skipped, second.ran) == (3, 2, 1)
    assert provider.calls == 5  # the missing recording fails at once, without the fallback
    failed = _results(run_dir)["T-9003"]
    assert failed.run_failed
    assert (failed.route, failed.route_reasons, failed.ticket_status) == (
        "handoff",
        ["system_error"],
        "handed_off",
    )
    assert failed.error == "FixtureMissingError"
    assert failed.draft is None
    metrics = json.loads((run_dir / files.METRICS_FILE).read_text())
    assert metrics["tickets_finished"] == 3
    assert metrics["runs_failed"] == 1
    assert "3 of 3" in (run_dir / files.REPORT_FILE).read_text()

    # The same tickets again from the start: every answer comes from the cache.
    third = await run(settings, dataclass_replace(options, restart=True), provider=provider)
    assert third.ran == 2
    # The earlier results are kept beside the new ones, never deleted.
    (kept,) = run_dir.glob("results.before-*.jsonl")
    assert len(files.read_results(kept)) == 3
    assert provider.calls == 5
    assert (third.cache_hits, third.cache_misses) == (4, 0)
    assert third.estimate is not None
    assert third.estimate.cached_calls == 4
    assert third.estimate.cost_usd == 0
    replayed = _results(run_dir)["T-9001"]
    assert replayed.paid_cost_usd == 0
    assert replayed.cost_usd == sepa.cost_usd
    assert replayed.draft == sepa.draft
    modes = run_sql(
        eval_db, "SELECT DISTINCT mode FROM llm_calls WHERE run_id = %s", replayed.run_id
    )
    assert modes == [("fake",)]

    # Without the cache the provider is called again.
    fourth = await run(
        settings, dataclass_replace(options, restart=True, cache=False, limit=1), provider=provider
    )
    assert fourth.ran == 1
    assert provider.calls == 7

    # Another ticket file is another run: refused without --restart.
    _tickets_file(tickets, [RUN_TICKETS["run_02"]])
    with pytest.raises(EvalSetupError, match="tickets_sha256"):
        await run(settings, options, provider=provider)


@dataclass
class DownProvider:
    """A provider that is down: every call fails."""

    calls: int = 0

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.calls += 1
        raise ProviderError("ServiceUnavailable", transient=False)

    async def embed(self, request: ProviderRequest) -> ProviderResponse:
        return await self.complete(request)


async def test_a_ticket_failed_by_an_outage_runs_again_on_resume(
    eval_db: Urls, tmp_path: Path
) -> None:
    tickets = _tickets_file(tmp_path / "tickets.jsonl", [RUN_TICKETS["run_01"]])
    options = Options(
        tools=False,
        tickets=tickets,
        variant=B0_VARIANT,
        run_dir=tmp_path / "run",
        cache_dir=tmp_path / "cache",
    )
    down = DownProvider()
    report = await run(_settings(eval_db), options, provider=down)
    assert down.calls == 2  # the model and its fallback
    assert (report.ran, report.unavailable) == (0, 1)
    assert files.read_results(tmp_path / "run" / files.RESULTS_FILE) == []
    resumed = await run(_settings(eval_db), options, provider=CountingProvider())
    assert (resumed.skipped, resumed.ran) == (0, 1)


async def test_the_estimate_above_the_limit_stops_before_any_call(
    eval_db: Urls, tmp_path: Path
) -> None:
    tickets = _tickets_file(tmp_path / "tickets.jsonl", [RUN_TICKETS["run_01"]])
    provider = CountingProvider()
    report = await run(
        _settings(eval_db, eval_max_cost_usd=0.0001),
        Options(
            tools=False,
            tickets=tickets,
            variant=B0_VARIANT,
            run_dir=tmp_path / "run",
            cache_dir=tmp_path / "cache",
        ),
        provider=provider,
    )
    assert report.stopped_by_estimate
    assert report.ran == 0
    assert provider.calls == 0
    assert files.read_results(tmp_path / "run" / files.RESULTS_FILE) == []
    assert json.loads((tmp_path / "run" / files.METRICS_FILE).read_text())["stopped_by_estimate"]


async def test_fake_mode_pays_nothing_and_ignores_the_limit(eval_db: Urls, tmp_path: Path) -> None:
    tickets = _tickets_file(tmp_path / "tickets.jsonl", [RUN_TICKETS["run_01"]])
    report = await run(
        _settings(eval_db, llm_mode="fake", eval_max_cost_usd=0),
        Options(
            tools=False,
            tickets=tickets,
            variant=B0_VARIANT,
            run_dir=tmp_path / "run",
            cache_dir=tmp_path / "cache",
        ),
    )
    assert report.ran == 1
    assert not report.stopped_by_estimate
    (result,) = files.read_results(tmp_path / "run" / files.RESULTS_FILE)
    assert result.route == "approval"
    assert result.paid_cost_usd == Decimal(0)
    assert (tmp_path / "cache" / "fake").is_dir()


@dataclass
class JudgingProvider:
    """The recordings for the answers; a fixed verdict for every judge call: no key fact
    present, no claim, citations not supported."""

    inner: CountingProvider = field(default_factory=CountingProvider)
    judge_calls: int = 0

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        verdicts: dict[str, dict[str, object]] = {
            "KeyFactsVerdict": {
                "facts": [{"fact": n, "quote": "", "present": False} for n in range(1, 4)]
            },
            "FaithfulnessVerdict": {"claims": []},
            "CitationVerdict": {"quote": "", "supports": False},
        }
        if request.schema_name in verdicts:
            self.judge_calls += 1
            return ProviderResponse(
                content=json.dumps(verdicts[request.schema_name]),
                usage=Usage(input_tokens=500, output_tokens=50),
            )
        return await self.inner.complete(request)

    async def embed(self, request: ProviderRequest) -> ProviderResponse:
        return await self.inner.embed(request)


async def test_judges_flags_and_metrics(eval_db: Urls, tmp_path: Path) -> None:
    tickets = _tickets_file(tmp_path / "tickets.jsonl", [RUN_TICKETS["run_01"]])
    base = load_tickets(tickets)[0]
    assert len(base.key_facts) == 3  # the fixed key-facts verdict has three items
    provider = JudgingProvider()
    run_dir = tmp_path / "run"
    options = Options(
        tools=False,
        tickets=tickets,
        variant=B0_VARIANT,
        run_dir=run_dir,
        cache_dir=tmp_path / "cache",
    )
    report = await run(_settings(eval_db), options, provider=provider)
    assert report.ran == 1
    assert report.estimate is not None
    assert report.estimate.calls > 2  # the judges are in the estimate

    (result,) = files.read_results(run_dir / files.RESULTS_FILE)
    assert result.draft is not None
    assert result.draft.can_answer
    # The triage flags come from the run's `triage.done` audit record.
    assert result.triage.flags() == {
        "wants_human": False,
        "reports_unauthorised_activity": False,
        "is_complaint_or_legal": False,
    }
    judges = result.judges
    assert judges is not None
    assert judges.errors == {}
    assert judges.key_facts is not None
    assert judges.key_facts.coverage == 0.0
    assert judges.faithfulness is not None
    # The fixed verdict lists no claim for a cited answer: counted unfaithful.
    assert judges.faithfulness.unchecked
    assert not judges.faithfulness.faithful
    assert len(judges.citations) >= 1
    assert judges.llm_calls == provider.judge_calls == 2 + len(judges.citations)
    assert judges.paid_cost_usd == judges.cost_usd > 0
    rows = run_sql(
        eval_db,
        "SELECT DISTINCT context, node, run_id IS NULL, prompt_version FROM llm_calls"
        " WHERE node = 'judge' ORDER BY prompt_version",
    )
    assert [row[3] for row in rows] == [
        "judge_citation/v3",
        "judge_faithfulness/v3",
        "judge_key_facts/v1",
    ]
    assert {row[:3] for row in rows} == {("eval", "judge", True)}

    metrics = json.loads((run_dir / files.METRICS_FILE).read_text())
    assert metrics["metrics"]["answers"]["key_fact_coverage"]["covered"] == 1
    assert metrics["judge_llm_calls"] == judges.llm_calls
    text = (run_dir / files.REPORT_FILE).read_text()
    assert "unvalidated, same model family" in text
    assert "key-fact coverage 0.00" in text
    assert "invalid citation" in text
    assert "no claim listed by the faithfulness judge" in text

    # A resume judges nothing again: the result is complete. Its limit starts from what the
    # written result paid, judges included.
    again = await run(_settings(eval_db), options, provider=provider)
    assert (again.skipped, again.ran) == (1, 0)
    assert provider.judge_calls == judges.llm_calls
    assert result.paid_total_usd > result.paid_cost_usd > 0
    assert again.spent_usd == result.paid_total_usd
    # Judges are part of the run's fingerprint.
    with pytest.raises(EvalSetupError, match="judge_prompt_versions"):
        await run(_settings(eval_db), dataclass_replace(options, judges=False), provider=provider)


def _paid(urls: Urls) -> Decimal:
    """All money paid in the evaluation database so far (`llm_calls` rows of mode `real`)."""
    ((total,),) = run_sql(
        urls, "SELECT coalesce(sum(cost_usd), 0) FROM llm_calls WHERE mode = 'real'"
    )
    return Decimal(total)


@dataclass
class JudgeDownProvider(JudgingProvider):
    """The answers and the key-facts judge work; the faithfulness judge and its fallback are
    down."""

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        if request.schema_name == "FaithfulnessVerdict":
            raise ProviderError("ServiceUnavailable", transient=False)
        return await super().complete(request)


async def test_a_judge_outage_is_not_written_and_its_paid_calls_count(
    eval_db: Urls, tmp_path: Path
) -> None:
    tickets = _tickets_file(tmp_path / "tickets.jsonl", [RUN_TICKETS["run_01"]])
    options = Options(
        tools=False,
        tickets=tickets,
        variant=B0_VARIANT,
        run_dir=tmp_path / "run",
        cache_dir=tmp_path / "cache",
    )
    before = _paid(eval_db)
    down = JudgeDownProvider()
    report = await run(_settings(eval_db), options, provider=down)
    assert (report.ran, report.unavailable) == (0, 1)
    assert down.judge_calls == 1  # key facts, then faithfulness is down
    assert files.read_results(tmp_path / "run" / files.RESULTS_FILE) == []
    # The limit has every paid call: the answer and the key-facts judge.
    assert report.spent_usd == _paid(eval_db) - before > 0

    # A resume judges again. The answer and the key-facts verdict come from the cache.
    provider = JudgingProvider()
    resumed = await run(_settings(eval_db), options, provider=provider)
    assert (resumed.ran, resumed.unavailable) == (1, 0)
    assert provider.inner.calls == 0
    (result,) = files.read_results(tmp_path / "run" / files.RESULTS_FILE)
    assert result.judges is not None
    assert provider.judge_calls == 1 + len(result.judges.citations)  # faithfulness, citations


@dataclass
class CostlyProvider(JudgingProvider):
    """Answers that cost far more than the estimate: the limit is reached before the judges."""

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        response = await super().complete(request)
        if request.schema_name in {"KeyFactsVerdict", "FaithfulnessVerdict", "CitationVerdict"}:
            return response
        return response.model_copy(update={"usage": Usage(input_tokens=5_000_000)})


async def test_the_limit_reached_before_the_judges_writes_nothing(
    eval_db: Urls, tmp_path: Path
) -> None:
    tickets = _tickets_file(tmp_path / "tickets.jsonl", [RUN_TICKETS["run_01"]])
    options = Options(
        tools=False,
        tickets=tickets,
        variant=B0_VARIANT,
        run_dir=tmp_path / "run",
        cache_dir=tmp_path / "cache",
    )
    provider = CostlyProvider()
    settings = _settings(eval_db, eval_max_cost_usd=1.0)
    report = await run(settings, options, provider=provider)
    assert not report.stopped_by_estimate
    assert report.stopped_at_cost_limit
    assert report.ran == 0
    assert provider.judge_calls == 0
    assert report.spent_usd > Decimal("1.0")
    assert files.read_results(tmp_path / "run" / files.RESULTS_FILE) == []


def dataclass_replace(options: Options, **changes: object) -> Options:
    import dataclasses

    return dataclasses.replace(options, **changes)  # type: ignore[arg-type]


# --- a RAG variant (P5-05) ----------------------------------------------------------------------

_SOURCE_ID = re.compile(r"^Source id: (\S+)$", re.MULTILINE)
SEPA_ANSWER = "A SEPA withdrawal arrives in 1 business day if you request it before 15:00 CET. [1]"


@dataclass
class ScriptedRagProvider(JudgingProvider):
    """Triage from the recordings, the judges' fixed verdicts, and a scripted writer and
    verifier: the draft cites the first chunk of its `<kb>`, the verifier finds the one claim
    there."""

    requests: list[ProviderRequest] = field(default_factory=list)

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        user = str(request.messages[-1]["content"])
        if request.schema_name == "DraftAnswer":
            first = _SOURCE_ID.findall(user)[0]
            content: dict[str, object] = {
                "can_answer": True,
                "missing_information": None,
                "answer": SEPA_ANSWER,
                "citations": [{"marker": 1, "source_id": first}],
                "proposed_actions": [],
            }
        elif request.schema_name == "ClaimsAnswer":
            first = _SOURCE_ID.findall(user)[0]
            content = {
                "claims": [{"claim": "SEPA takes 1 day", "supported": True, "source_id": first}]
            }
        else:
            return await super().complete(request)
        return ProviderResponse(
            content=json.dumps(content), usage=Usage(input_tokens=1000, output_tokens=100)
        )


async def _aliases(qdrant_url: str) -> dict[str, str]:
    from qdrant_client import AsyncQdrantClient

    client = AsyncQdrantClient(url=qdrant_url)
    try:
        response = await client.get_aliases()
        return {item.alias_name: item.collection_name for item in response.aliases}
    finally:
        await client.close()


async def test_a_rag_ticket_runs_through_the_runner(
    eval_db: Urls, qdrant_url: str, tmp_path: Path
) -> None:
    """P5-05 done when: one RAG ticket through the runner with a scripted provider. The runner
    indexes the evaluation KB under its own alias, the writer reads the search hits, verify
    runs, the judges read the chunks the writer read, and the report shows hit@5."""
    from hub_evals.runner import EVAL_KB_ALIAS

    tickets = _tickets_file(tmp_path / "tickets.jsonl", [RUN_TICKETS["run_01"]])
    run_dir = tmp_path / "run"
    options = Options(tools=False, tickets=tickets, run_dir=run_dir, cache_dir=tmp_path / "cache")
    assert options.variant == "rag_hybrid"  # the default
    settings = _settings(eval_db, qdrant_url=qdrant_url, embedding_cache_dir=MODEL_DIR)
    app_aliases = {k: v for k, v in (await _aliases(qdrant_url)).items() if k != EVAL_KB_ALIAS}
    provider = ScriptedRagProvider()

    report = await run(settings, options, provider=provider)

    assert report.ran == 1
    assert report.estimate is not None
    # triage, write, verify, and the judges: faithfulness, citations, key facts
    assert report.estimate.calls == 3 + 1 + ESTIMATED_CITATIONS + 1
    aliases = await _aliases(qdrant_url)
    assert aliases[EVAL_KB_ALIAS].startswith("eval_kb_chunks_v")
    # The app's index is never touched.
    assert {k: v for k, v in aliases.items() if k != EVAL_KB_ALIAS} == app_aliases

    (result,) = files.read_results(run_dir / files.RESULTS_FILE)
    assert result.prompt_versions == {"triage": "v1", "write": "v6", "verify": "v1"}
    assert result.draft is not None
    assert result.draft.evidence["variant"] == "rag_hybrid"
    ranked = result.draft.ranked_chunk_ids
    assert ranked is not None
    assert len(ranked) == 6
    first = ranked[0]
    assert result.draft.citations[0]["source_id"] == first
    assert (result.draft.groundedness, result.draft.unsupported_claims) == (Decimal("1.000"), [])
    nodes = run_sql(
        eval_db,
        "SELECT node FROM llm_calls WHERE run_id = %s ORDER BY created_at",
        result.run_id,
    )
    assert [node for (node,) in nodes] == ["triage", "write", "verify"]
    embeddings = run_sql(
        eval_db,
        "SELECT DISTINCT context, sum(cost_usd) FROM llm_calls WHERE node = 'embedding'"
        " GROUP BY context",
    )
    assert embeddings == [("eval", Decimal(0))]

    # The judges read what the writer read: every chunk for faithfulness, the cited one for
    # the citation.
    judged = {r.schema_name: str(r.messages[-1]["content"]) for r in provider.requests}
    assert _SOURCE_ID.findall(judged["FaithfulnessVerdict"]) == ranked
    assert _SOURCE_ID.findall(judged["CitationVerdict"]) == [first]
    assert result.judges is not None
    assert [c.source_id for c in result.judges.citations] == [first]

    metrics = json.loads((run_dir / files.METRICS_FILE).read_text())["metrics"]
    assert metrics["retrieval"]["covered"] == 1
    assert metrics["retrieval"]["hit_at_5"]["total"] == 1
    assert "| hit@5 | " in (run_dir / files.REPORT_FILE).read_text()

    # A second run finds the index in line and embeds no chunk again: only the query.
    before = run_sql(eval_db, "SELECT count(*) FROM llm_calls WHERE node = 'embedding'")[0][0]
    again = await run(settings, dataclass_replace(options, restart=True), provider=provider)
    assert again.ran == 1
    after = run_sql(eval_db, "SELECT count(*) FROM llm_calls WHERE node = 'embedding'")[0][0]
    assert after - before <= 2  # the estimate's search and the run's search


# --- the start state of a run (P12) -------------------------------------------------------------


async def _leftover_tickets(urls: Urls, customer_ref: str, intent: str) -> list[uuid.UUID]:
    """One ticket of `customer_ref` with `intent` in every status, as earlier starts leave them,
    made through the checked status path."""
    paths: dict[S, list[S]] = {
        S.PROCESSING: [],
        S.PENDING_APPROVAL: [S.PENDING_APPROVAL],
        S.HANDED_OFF: [S.HANDED_OFF],
        S.ANSWERED: [S.ANSWERED],
        S.CLOSED: [S.ANSWERED, S.CLOSED],
    }
    database = connect(urls[1])
    try:
        async with database.sessions.begin() as session:
            customer = await CustomerRepository(session).get_by_external_ref(customer_ref)
            assert customer is not None
            tickets = TicketRepository(session)
            ids: list[uuid.UUID] = []
            for path in paths.values():
                row = await tickets.create(
                    customer_id=customer.id, ai_disclosed_at=datetime.now(UTC)
                )
                row.intent = Intent(intent)
                for target in path:
                    await tickets.change_status(row, target)
                ids.append(row.id)
            return ids
    finally:
        await database.dispose()


async def _repeat_contact(urls: Urls, customer_ref: str, intent: str) -> bool:
    """What `decide` reads for a new ticket of the customer: `memory_shows_open_case`."""
    database = connect(urls[1])
    try:
        async with database.sessions() as session:
            customer = await CustomerRepository(session).get_by_external_ref(customer_ref)
            assert customer is not None
            context = await customer_context(session, customer.id, uuid.uuid4())
            return shows_open_case(context, intent, datetime.now(UTC))
    finally:
        await database.dispose()


async def test_a_start_closes_the_open_tickets_of_earlier_starts(
    eval_db: Urls, tmp_path: Path
) -> None:
    """P12: leftover open tickets made every evaluation ticket a repeat contact
    (`policy_review`, automation share 0 of 20 in P12-00). Each start closes them first, through
    the state diagram, so the routing rule itself stays as it is."""
    tickets = _tickets_file(tmp_path / "tickets.jsonl", [RUN_TICKETS["run_01"]])
    base = load_tickets(tickets)[0]
    options = Options(
        tools=False,
        tickets=tickets,
        variant=B0_VARIANT,
        run_dir=tmp_path / "run",
        cache_dir=tmp_path / "cache",
        judges=False,
    )
    settings = _settings(eval_db, llm_mode="fake")
    await run(settings, options)  # seeds the customers
    leftovers = await _leftover_tickets(eval_db, base.customer_ref, "withdrawal_issue")
    assert await _repeat_contact(eval_db, base.customer_ref, "withdrawal_issue")
    open_before = run_sql(eval_db, "SELECT count(*) FROM tickets WHERE status <> 'closed'")[0][0]
    assert open_before >= 4

    report = await run(settings, dataclass_replace(options, restart=True))

    assert report.ran == 1
    assert report.tickets_closed == open_before
    statuses = run_sql(
        eval_db,
        "SELECT status FROM tickets WHERE id = ANY(%s)",
        leftovers,
    )
    assert {status for (status,) in statuses} == {"closed"}
    # Only the ticket of this start is open, so the next start of the same customer and intent
    # is not a repeat contact of any earlier one once it closes the open ones again.
    assert run_sql(eval_db, "SELECT count(*) FROM tickets WHERE status <> 'closed'") == [(1,)]
    database = connect(eval_db[1])
    try:
        assert await close_open_tickets(database) == 1
        assert await close_open_tickets(database) == 0
    finally:
        await database.dispose()
    assert not await _repeat_contact(eval_db, base.customer_ref, "withdrawal_issue")
    # One audit record per closed ticket, with where it came from; the earlier statuses all
    # reached `closed` through allowed steps.
    audited = run_sql(
        eval_db,
        "SELECT details->>'from_status' FROM audit_log WHERE action = 'ticket.closed'"
        " AND details->>'job' = 'eval-reset' AND entity_id = ANY(%s)",
        [str(ticket_id) for ticket_id in leftovers],
    )
    assert sorted(status for (status,) in audited) == [
        "answered",
        "handed_off",
        "pending_approval",
        "processing",
    ]
