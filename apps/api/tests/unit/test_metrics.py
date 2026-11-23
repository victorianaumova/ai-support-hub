"""Pure parts of the dashboard metrics (P10-09): the period rule, shares and the eval reader."""

import json
from datetime import date
from pathlib import Path

import pytest

from hub.metrics.dashboard import MAX_DAYS, PeriodError, period, share
from hub.metrics.evals import latest, read_run

TODAY = date(2026, 11, 23)


def test_the_default_period_is_the_last_seven_days_with_today() -> None:
    chosen = period(None, None, TODAY)
    assert (chosen.start, chosen.end) == (date(2026, 11, 17), TODAY)
    assert len(chosen.days()) == 7


def test_one_end_fills_the_other() -> None:
    assert period(date(2026, 11, 1), None, TODAY).end == date(2026, 11, 7)
    assert period(None, date(2026, 11, 7), TODAY).start == date(2026, 11, 1)


def test_the_period_bounds_are_whole_utc_days() -> None:
    chosen = period(date(2026, 11, 10), date(2026, 11, 12), TODAY)
    assert chosen.lower.isoformat() == "2026-11-10T00:00:00+00:00"
    assert chosen.upper.isoformat() == "2026-11-13T00:00:00+00:00"


def test_a_reversed_or_too_long_period_is_refused() -> None:
    with pytest.raises(PeriodError):
        period(date(2026, 11, 12), date(2026, 11, 10), TODAY)
    with pytest.raises(PeriodError):
        period(date(2025, 1, 1), date(2026, 1, 2), TODAY)
    start = date(2025, 11, 23)
    assert len(period(start, date.fromordinal(start.toordinal() + MAX_DAYS - 1), TODAY).days()) == (
        MAX_DAYS
    )


def test_a_share_of_nothing_is_none() -> None:
    assert share(0, 0) is None
    assert share(1, 3) == 0.3333


def _write(folder: Path, config: object, metrics: object) -> None:
    folder.mkdir(parents=True)
    (folder / "config.json").write_text(json.dumps(config))
    (folder / "metrics.json").write_text(json.dumps(metrics))


def test_an_unreadable_run_folder_is_skipped(tmp_path: Path) -> None:
    good = {"variant": "rag_hybrid", "llm_mode": "real", "git_sha": "a", "git_dirty": False}
    _write(
        tmp_path / "2026-11-20-rag_hybrid-a",
        good,
        {"tickets_finished": 1, "cost_usd": "0", "checks": []},
    )
    _write(tmp_path / "2026-11-21-rag_hybrid-b", good, {"checks": "not a list"})
    (tmp_path / "2026-11-22-rag_hybrid-c").mkdir()
    (tmp_path / "2026-11-22-rag_hybrid-c" / "config.json").write_text("{broken")
    (tmp_path / "notes").mkdir()

    found = latest(tmp_path)

    assert found is not None
    assert found.run_id == "2026-11-20-rag_hybrid-a"


def test_a_missing_runs_folder_has_no_latest_run(tmp_path: Path) -> None:
    assert latest(tmp_path / "missing") is None
    assert read_run(tmp_path / "not-a-date") is None


def test_the_committed_runs_have_a_latest_run() -> None:
    """The repository's own reports parse, so the dashboard box is not empty after a deploy."""
    found = latest(Path(__file__).parents[4] / "evals" / "runs")
    assert found is not None
    assert found.checks
