"""Run tasks for several trials, grade each, and summarise against the spec's goals.

- pass@1: the mean per-task pass rate.
- pass^k: the share of tasks where every one of the k trials passed (Anthropic's
  measure of consistency).

A trial whose replay missed the tape ran in an incomplete environment, so its failure
may not be the agent's. The summary counts such trials and sets `comparable` false
rather than dropping them silently. Results go to `out_path`, which must sit outside
the repository: transcripts carry real customer data.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import asdict
from pathlib import Path

from app.services.benchmarks.resolve.graders import grade


def summarize(rows: list[dict], *, trials: int) -> dict:
    by_task: dict[str, list[bool]] = {}
    for row in rows:
        by_task.setdefault(row["ref"], []).append(bool(row["outcome_ok"]))
    rates = [sum(passes) / len(passes) for passes in by_task.values()]
    resolved_tokens = [row["tokens"] for row in rows if row.get("outcome_ok") and "tokens" in row]
    words = [row["words"] for row in rows if "words" in row]
    incomplete = sum(1 for row in rows if row.get("environment_complete") is False)
    return {
        "tasks": len(by_task),
        "trials": trials,
        "g1_pass_at_1": statistics.fmean(rates) if rates else 0.0,
        "g1_pass_hat_k": (
            sum(1 for passes in by_task.values() if len(passes) == trials and all(passes)) / len(by_task)
            if by_task
            else 0.0
        ),
        "g3_safety_violations": sum(row.get("safety_violations", 0) for row in rows),
        "g4_median_words": statistics.median(words) if words else None,
        # The target is zero, so the total (stricter than a median) is reported, with how many trials had any.
        "g4_model_amounts": sum(len(row.get("model_amounts", [])) for row in rows),
        "g4_trials_with_amounts": sum(1 for row in rows if row.get("model_amounts")),
        "g4_median_amounts": statistics.median(len(row.get("model_amounts", [])) for row in rows) if rows else None,
        "g5_median_tokens_resolved": statistics.median(resolved_tokens) if resolved_tokens else None,
        "environment_incomplete_trials": incomplete,
        "comparable": incomplete == 0,
    }


def _save(out_path, meta, rows, trials) -> dict:
    summary = summarize(rows, trials=trials)
    if out_path is not None:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        body = {"meta": meta or {}, "summary": summary, "trials": rows}
        Path(out_path).write_text(json.dumps(body, indent=2, default=str))
    return summary


async def run(tasks, agent, *, trials: int = 3, out_path=None, meta: dict | None = None, interpret=None) -> dict:
    """`agent(task, trial) -> Attempt`. Trials run one after another so tokens and wall time stay honest.

    `interpret(text) -> {diagnosis, action} | None` (async) reads what the person saw when
    the agent declared no resolution. A failed interpretation is recorded on that trial
    (which then has no diagnosis) and the run goes on. Results are saved after every
    trial, so a crash keeps the work done.
    """
    rows = []
    for task in tasks:
        for trial in range(trials):
            attempt = await agent(task, trial)
            reading, interpret_error = None, None
            if attempt.resolution is None and interpret is not None:
                try:
                    reading = await interpret(attempt.shown_text or attempt.reply_text)
                except Exception as exc:  # noqa: BLE001 - one provider failure must not lose the run
                    interpret_error = f"{type(exc).__name__}: {exc}"
            hook = (lambda _text: reading) if interpret is not None else None
            graded = asdict(grade(task, attempt, interpret=hook))
            rows.append(
                {
                    "ref": task.ref,
                    "trial": trial,
                    **graded,
                    "reply_text": attempt.reply_text,
                    "error": attempt.error,
                    "interpret_error": interpret_error,
                }
            )
            _save(out_path, meta, rows, trials)
    return _save(out_path, meta, rows, trials)
