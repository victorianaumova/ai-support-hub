"""The newest committed evaluation report for the dashboard (`GET /console/evals/latest`, P10-09).

Reads `metrics.json` and `config.json` of the run folders in `EVAL_RUNS_DIR` (eval plan §4: both
are committed, `results.jsonl` is not). Only a clean real-model run of the dev tickets counts:
attack runs, fake-mode checks (`-fake`) and runs with uncommitted changes (`-dirty`, or
`git_dirty` in the config) are skipped. Newest = latest date in the folder name; on the same
date, the folder whose `metrics.json` was written last, then the name. A folder that cannot be
read is skipped, never an error: the dashboard shows "no evaluation" instead.
"""

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)


class EvalCheck(BaseModel):
    name: str
    shown: str
    target_text: str
    red_line_text: str
    # "pass", "below target", "red line missed" or "reported" (eval plan §2).
    status: str
    judged: bool


class EvalSummary(BaseModel):
    run_id: str
    date: date
    variant: str
    git_sha: str
    tickets_finished: int
    cost_usd: str
    checks: list[EvalCheck]


def _skipped_name(name: str) -> bool:
    return "-attacks-" in name or name.endswith(("-dirty", "-fake"))


def _run_date(name: str) -> date | None:
    try:
        return date.fromisoformat(name[:10])
    except ValueError:
        return None


def read_run(folder: Path) -> EvalSummary | None:
    """The summary of one run folder, or `None` when it is not a clean real-model run."""
    run_date = _run_date(folder.name)
    if run_date is None or _skipped_name(folder.name):
        return None
    try:
        config: dict[str, Any] = json.loads((folder / "config.json").read_text("utf-8"))
        metrics: dict[str, Any] = json.loads((folder / "metrics.json").read_text("utf-8"))
        if config.get("llm_mode") != "real" or config.get("git_dirty") is not False:
            return None
        return EvalSummary(
            run_id=folder.name,
            date=run_date,
            variant=config["variant"],
            git_sha=config["git_sha"],
            tickets_finished=metrics["tickets_finished"],
            cost_usd=str(metrics["cost_usd"]),
            checks=[EvalCheck.model_validate(check) for check in metrics["checks"]],
        )
    except (OSError, ValueError, KeyError, TypeError, ValidationError):
        logger.warning("evaluation run folder skipped: not readable", extra={"run": folder.name})
        return None


def _order(folder: Path) -> tuple[str, float, str]:
    try:
        written = (folder / "metrics.json").stat().st_mtime
    except OSError:
        written = 0.0
    return folder.name[:10], written, folder.name


def latest(runs_dir: Path) -> EvalSummary | None:
    if not runs_dir.is_dir():
        return None
    for folder in sorted((p for p in runs_dir.iterdir() if p.is_dir()), key=_order, reverse=True):
        found = read_run(folder)
        if found is not None:
            return found
    return None
