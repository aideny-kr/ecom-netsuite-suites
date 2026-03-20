# Autoresearch Loop — SuiteStudio Agent Optimization

> Inspired by [Karpathy's autoresearch](https://github.com/karpathy/autoresearch/discussions/43).
> Instead of optimizing model weights, we optimize agent prompts, SuiteQL rules, and tool selection heuristics.

## Concept

A scheduled job runs 2-3x/day. Each run:
1. Loads the current golden query baseline (30 test cases, cached NetSuite responses)
2. Picks an experiment hypothesis (AI-generated or from a backlog)
3. Modifies agent rules in a git branch
4. Runs the golden regression suite
5. Compares metrics: pass rate, judge confidence, token usage
6. If improved or neutral → commits. If regressed → discards.
7. Posts a session report to `autoresearch/sessions/`

## Scope (Phase 1 — Minimal)

- **Metric**: Golden query pass rate (30 queries × 5 test cycles = 150 assertions)
- **Experiments**: SuiteQL rule tweaks, prompt wording, tool selection guidance
- **Environment**: Hybrid — cached responses for regression, live sandbox for new hypothesis validation
- **Safety**: All changes in a branch, never auto-merges to main

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   Scheduled Task (cron)                   │
│                  runs 2-3x/day via Celery                │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│              1. BASELINE                                  │
│  Run golden regression suite → record pass/fail + scores │
│  Cache: knowledge/golden_query_responses.json            │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│              2. HYPOTHESIZE                               │
│  LLM reads: current rules + recent failures + session    │
│  history → proposes ONE targeted change                  │
│  Output: { hypothesis, file, diff_description }          │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│              3. EXPERIMENT                                │
│  Apply the diff to a git worktree (isolated branch)      │
│  Run golden regression suite again                       │
│  Record: pass/fail + scores + token delta                │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│              4. EVALUATE                                  │
│  Compare experiment vs baseline:                         │
│  - pass_rate: must not decrease                          │
│  - judge_confidence_avg: should not decrease             │
│  - token_usage: lower is better (tiebreaker)             │
│  Decision: KEEP or DISCARD                               │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│              5. REPORT                                    │
│  Write session report to autoresearch/sessions/          │
│  Include: hypothesis, diff, metrics before/after,        │
│  decision (keep/discard), cumulative improvement          │
│  If KEEP → commit to autoresearch branch                 │
└─────────────────────────────────────────────────────────┘
```

## File Structure

```
autoresearch/
├── config.json                    # Experiment config (max_experiments_per_session, etc.)
├── backlog.json                   # Manual experiment ideas (optional)
├── sessions/                      # One file per run
│   ├── 2026-03-14_06-00.json     # Morning run
│   ├── 2026-03-14_14-00.json     # Afternoon run
│   └── 2026-03-14_22-00.json     # Evening run
├── cumulative.json                # Running tally: baseline → current best
└── cache/
    └── golden_responses.json      # Cached NetSuite API responses for offline replay
```

## Implementation

### 1. Golden Query Response Cache

Cache real NetSuite responses so experiments run offline (zero API cost, fast):

```python
# scripts/autoresearch/cache_golden_responses.py
"""Run each golden query against live NetSuite and cache the response.
Run manually once, then experiments replay cached responses."""

import json
from knowledge.golden_queries import GOLDEN_QUERIES

async def cache_responses():
    cache = {}
    for gq in GOLDEN_QUERIES:
        # Execute the expected_sql against live NetSuite
        result = await execute_suiteql(access_token, account_id, gq["expected_sql"])
        cache[gq["id"]] = {
            "query": gq["expected_sql"],
            "response": result,
            "cached_at": datetime.utcnow().isoformat(),
        }

    with open("autoresearch/cache/golden_responses.json", "w") as f:
        json.dump(cache, f, indent=2, default=str)
```

### 2. Baseline Runner

```python
# scripts/autoresearch/baseline.py
"""Run the golden regression suite and return structured metrics."""

import subprocess
import json
import re

def run_baseline() -> dict:
    """Execute pytest and parse results into metrics."""
    result = subprocess.run(
        ["python", "-m", "pytest", "backend/tests/test_golden_query_regression.py", "-v", "--tb=short", "-q"],
        capture_output=True, text=True, cwd=PROJECT_ROOT,
    )

    # Parse pytest output
    passed = len(re.findall(r"PASSED", result.stdout))
    failed = len(re.findall(r"FAILED", result.stdout))
    errors = len(re.findall(r"ERROR", result.stdout))

    # Extract specific test results
    test_results = parse_pytest_verbose(result.stdout)

    return {
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "pass_rate": passed / (passed + failed + errors) if (passed + failed + errors) > 0 else 0,
        "test_results": test_results,
        "raw_output": result.stdout[-2000:],  # Last 2K chars for debugging
    }
```

### 3. Hypothesis Generator

The core intelligence — uses an LLM to propose experiments:

```python
# scripts/autoresearch/hypothesize.py
"""Generate a single experiment hypothesis using Claude."""

HYPOTHESIS_PROMPT = """You are an agent optimization researcher. You are given:

1. The current SuiteQL agent rules (the system prompt that guides query generation)
2. Recent test failures (golden queries that failed in the last baseline run)
3. Session history (what experiments were already tried and their outcomes)

Your job: propose ONE small, targeted change to improve the agent's accuracy.

RULES:
- Change ONLY ONE thing at a time (a single rule addition, modification, or reword)
- The change must be in unified_agent.py or suiteql_agent.py (keep them in sync)
- Focus on the MOST IMPACTFUL failure first
- If all tests pass, propose a token reduction (shorten verbose rules without losing meaning)
- Never remove a rule that was added by a previous successful experiment
- Output a JSON object with: hypothesis, target_file, section, old_text, new_text, rationale

<current_rules>
{current_rules}
</current_rules>

<recent_failures>
{recent_failures}
</recent_failures>

<session_history>
{session_history}
</session_history>
"""

async def generate_hypothesis(
    current_rules: str,
    recent_failures: list[dict],
    session_history: list[dict],
) -> dict:
    """Ask Claude to propose one experiment."""
    import anthropic

    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": HYPOTHESIS_PROMPT.format(
                current_rules=current_rules,
                recent_failures=json.dumps(recent_failures, indent=2),
                session_history=json.dumps(session_history[-10:], indent=2),  # Last 10 experiments
            ),
        }],
    )

    # Parse JSON from response
    text = response.content[0].text
    return json.loads(extract_json_block(text))
```

### 4. Experiment Runner

```python
# scripts/autoresearch/experiment.py
"""Apply a hypothesis diff and run the regression suite."""

import subprocess
import shutil
from pathlib import Path

def run_experiment(hypothesis: dict, project_root: Path) -> dict:
    """Apply the hypothesis, run tests, return results."""

    # Create a git worktree for isolation
    branch_name = f"autoresearch/{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    worktree_path = project_root / ".autoresearch_worktree"

    subprocess.run(["git", "worktree", "add", str(worktree_path), "-b", branch_name], cwd=project_root)

    try:
        # Apply the diff
        target_file = worktree_path / hypothesis["target_file"]
        content = target_file.read_text()

        if hypothesis["old_text"] not in content:
            return {"error": "old_text not found in target file", "decision": "DISCARD"}

        new_content = content.replace(hypothesis["old_text"], hypothesis["new_text"], 1)
        target_file.write_text(new_content)

        # If unified_agent.py changed, mirror to suiteql_agent.py (or vice versa)
        if "unified_agent" in hypothesis["target_file"]:
            _mirror_change(worktree_path, "suiteql_agent.py", hypothesis)
        elif "suiteql_agent" in hypothesis["target_file"]:
            _mirror_change(worktree_path, "unified_agent.py", hypothesis)

        # Run regression suite in the worktree
        result = subprocess.run(
            ["python", "-m", "pytest", "backend/tests/test_golden_query_regression.py", "-v", "--tb=short", "-q"],
            capture_output=True, text=True, cwd=worktree_path,
        )

        metrics = parse_pytest_results(result.stdout)
        return {
            "metrics": metrics,
            "branch": branch_name,
            "diff": hypothesis,
        }

    finally:
        # Clean up worktree
        subprocess.run(["git", "worktree", "remove", str(worktree_path), "--force"], cwd=project_root)
```

### 5. Evaluator

```python
# scripts/autoresearch/evaluate.py
"""Compare experiment results against baseline. Decide KEEP or DISCARD."""

def evaluate(baseline: dict, experiment: dict) -> dict:
    """Apply the evaluation criteria."""

    b = baseline
    e = experiment["metrics"]

    # Hard constraint: pass rate must not decrease
    if e["pass_rate"] < b["pass_rate"]:
        return {
            "decision": "DISCARD",
            "reason": f"Pass rate decreased: {b['pass_rate']:.1%} → {e['pass_rate']:.1%}",
        }

    # Hard constraint: no new failures
    if e["failed"] > b["failed"]:
        new_failures = set(e.get("failed_tests", [])) - set(b.get("failed_tests", []))
        return {
            "decision": "DISCARD",
            "reason": f"New failures: {new_failures}",
        }

    # Improvement: pass rate increased
    if e["pass_rate"] > b["pass_rate"]:
        return {
            "decision": "KEEP",
            "reason": f"Pass rate improved: {b['pass_rate']:.1%} → {e['pass_rate']:.1%}",
            "improvement": e["pass_rate"] - b["pass_rate"],
        }

    # Neutral: same pass rate, check token usage
    # (Lower tokens = more efficient prompt = keep)
    if e.get("total_tokens", 0) < b.get("total_tokens", float("inf")) * 0.95:
        return {
            "decision": "KEEP",
            "reason": f"Same accuracy, {((b['total_tokens'] - e['total_tokens']) / b['total_tokens']):.0%} fewer tokens",
        }

    return {
        "decision": "DISCARD",
        "reason": "No measurable improvement",
    }
```

### 6. Session Report

```python
# scripts/autoresearch/report.py
"""Generate and save the session report."""

def generate_report(
    session_id: str,
    baseline: dict,
    experiments: list[dict],
    cumulative: dict,
) -> dict:
    report = {
        "session_id": session_id,
        "started_at": datetime.utcnow().isoformat(),
        "baseline": {
            "pass_rate": baseline["pass_rate"],
            "passed": baseline["passed"],
            "failed": baseline["failed"],
        },
        "experiments": [],
        "summary": {
            "total_experiments": len(experiments),
            "kept": 0,
            "discarded": 0,
            "cumulative_improvement": 0,
        },
    }

    for exp in experiments:
        entry = {
            "hypothesis": exp["diff"]["hypothesis"],
            "rationale": exp["diff"]["rationale"],
            "decision": exp["evaluation"]["decision"],
            "reason": exp["evaluation"]["reason"],
            "pass_rate_before": baseline["pass_rate"],
            "pass_rate_after": exp["metrics"]["pass_rate"],
        }
        report["experiments"].append(entry)

        if exp["evaluation"]["decision"] == "KEEP":
            report["summary"]["kept"] += 1
        else:
            report["summary"]["discarded"] += 1

    return report
```

### 7. Main Loop (Celery Task)

```python
# scripts/autoresearch/run.py
"""Main autoresearch loop — runs as a scheduled Celery task."""

import json
from pathlib import Path
from datetime import datetime

MAX_EXPERIMENTS_PER_SESSION = 5  # Keep it small — quality over quantity
PROJECT_ROOT = Path(__file__).resolve().parents[2]


async def run_autoresearch_session():
    """One complete autoresearch session."""
    session_id = datetime.utcnow().strftime("%Y-%m-%d_%H-%M")

    # 1. Baseline
    print(f"[AUTORESEARCH] Session {session_id} — running baseline...")
    baseline = run_baseline()
    print(f"[AUTORESEARCH] Baseline: {baseline['pass_rate']:.1%} ({baseline['passed']}/{baseline['passed'] + baseline['failed']})")

    # 2. Load session history (last 20 experiments for context)
    history = load_session_history(limit=20)

    # 3. Load current agent rules
    unified_rules = (PROJECT_ROOT / "backend/app/services/chat/agents/unified_agent.py").read_text()

    experiments = []

    for i in range(MAX_EXPERIMENTS_PER_SESSION):
        print(f"[AUTORESEARCH] Experiment {i+1}/{MAX_EXPERIMENTS_PER_SESSION}")

        # 3. Hypothesize
        try:
            hypothesis = await generate_hypothesis(
                current_rules=unified_rules,
                recent_failures=baseline.get("failed_tests", []),
                session_history=history + [e["diff"] for e in experiments],
            )
        except Exception as e:
            print(f"[AUTORESEARCH] Hypothesis generation failed: {e}")
            break

        print(f"[AUTORESEARCH] Hypothesis: {hypothesis['hypothesis']}")

        # 4. Experiment
        result = run_experiment(hypothesis, PROJECT_ROOT)
        if result.get("error"):
            print(f"[AUTORESEARCH] Experiment failed: {result['error']}")
            experiments.append({"diff": hypothesis, "metrics": {}, "evaluation": {"decision": "DISCARD", "reason": result["error"]}})
            continue

        # 5. Evaluate
        evaluation = evaluate(baseline, result)
        result["evaluation"] = evaluation
        experiments.append(result)

        print(f"[AUTORESEARCH] Decision: {evaluation['decision']} — {evaluation['reason']}")

        # 6. If KEEP, apply to main worktree and update baseline
        if evaluation["decision"] == "KEEP":
            apply_to_main(hypothesis, PROJECT_ROOT)
            # Re-run baseline with the improvement applied
            baseline = run_baseline()
            unified_rules = (PROJECT_ROOT / "backend/app/services/chat/agents/unified_agent.py").read_text()

    # 7. Report
    report = generate_report(session_id, baseline, experiments, load_cumulative())
    save_session_report(session_id, report)
    update_cumulative(report)

    kept = report["summary"]["kept"]
    total = report["summary"]["total_experiments"]
    print(f"[AUTORESEARCH] Session complete: {kept}/{total} experiments kept")

    return report
```

### 8. Celery Task Registration

```python
# backend/app/workers/tasks/autoresearch.py
"""Celery task for scheduled autoresearch runs."""

import asyncio
from app.workers.celery_app import celery_app

@celery_app.task(name="tasks.autoresearch", queue="research")
def run_autoresearch():
    """Run one autoresearch session."""
    from scripts.autoresearch.run import run_autoresearch_session
    loop = asyncio.new_event_loop()
    try:
        report = loop.run_until_complete(run_autoresearch_session())
        return {
            "session_id": report["session_id"],
            "kept": report["summary"]["kept"],
            "discarded": report["summary"]["discarded"],
        }
    finally:
        loop.close()
```

### 9. Celery Beat Schedule

```python
# In celery_app.py or celery config:
celery_app.conf.beat_schedule = {
    "autoresearch-morning": {
        "task": "tasks.autoresearch",
        "schedule": crontab(hour=6, minute=0),   # 6 AM UTC
    },
    "autoresearch-afternoon": {
        "task": "tasks.autoresearch",
        "schedule": crontab(hour=14, minute=0),  # 2 PM UTC
    },
    "autoresearch-evening": {
        "task": "tasks.autoresearch",
        "schedule": crontab(hour=22, minute=0),  # 10 PM UTC
    },
}
```

## Safety Guardrails

### Hard rules (never violated)
1. **Never auto-merge to main** — kept experiments commit to `autoresearch/*` branches
2. **Never decrease pass rate** — any regression = instant DISCARD
3. **Never remove existing rules** — only add, modify wording, or reorder
4. **Max 5 experiments per session** — prevents runaway cost
5. **Git worktree isolation** — experiments can't corrupt the working tree
6. **Dual-file sync enforced** — unified_agent.py and suiteql_agent.py always updated together

### Soft rules (LLM-enforced via hypothesis prompt)
1. Change only ONE thing per experiment
2. Focus on most impactful failure first
3. If all tests pass, optimize for token reduction
4. Don't repeat experiments that were already discarded

### Rollback
- Each session's report includes the exact diff applied
- `git revert` any autoresearch commit by session ID
- Cumulative tracker shows which experiments contributed to current state

## Cost Estimate

Per session (5 experiments max):
- Hypothesis generation: 5 × ~2K input + ~500 output tokens = ~$0.04 (Sonnet)
- Regression suite: pytest only, no API calls (cached responses)
- Live validation (optional, Phase 2): 1-2 queries × $0.003 each

**Total: ~$0.05-0.10 per session, ~$0.15-0.30 per day**

## Metrics Dashboard (Phase 2)

Track cumulative improvement over time:

```json
// autoresearch/cumulative.json
{
  "initial_baseline": { "pass_rate": 0.87, "date": "2026-03-14" },
  "current_best": { "pass_rate": 0.93, "date": "2026-03-20" },
  "total_sessions": 18,
  "total_experiments": 72,
  "total_kept": 11,
  "total_discarded": 61,
  "improvements": [
    { "date": "2026-03-14", "hypothesis": "Add BUILTIN.DF() guidance for status display", "delta": "+0.033" },
    { "date": "2026-03-15", "hypothesis": "Clarify assemblycomponent filter for BOM queries", "delta": "+0.017" },
  ]
}
```

## Live Experiment Discovery (Phase 2)

Once cached regression is stable, add a live discovery path:

1. Pull recent chat failures from `chat_messages` where `confidence_score < 3.0`
2. Replay the user's question against live NetSuite with the proposed rule change
3. Compare: did the new rule produce a correct query?
4. If yes → add to golden_queries.json as a new test case + keep the rule
5. If no → discard

This is the "autoresearch grows its own test suite" mode — the golden query count increases over time.

## Implementation Order

1. **`scripts/autoresearch/cache_golden_responses.py`** — Cache responses (run once manually)
2. **`scripts/autoresearch/baseline.py`** — Baseline runner
3. **`scripts/autoresearch/hypothesize.py`** — LLM hypothesis generator
4. **`scripts/autoresearch/experiment.py`** — Apply + test in worktree
5. **`scripts/autoresearch/evaluate.py`** — Compare metrics
6. **`scripts/autoresearch/report.py`** — Session report generator
7. **`scripts/autoresearch/run.py`** — Main loop
8. **Celery task + beat schedule** — Wire up the cron
9. **Test with 1 manual session** — Verify end-to-end before enabling cron
10. **Enable cron** — 3x/day

## Open Questions

- Should KEEP'd experiments auto-create a PR for human review, or just commit to the autoresearch branch?
- Should we add a Slack/email notification when a session finds an improvement?
- Should Phase 2 (live discovery) use a separate NetSuite sandbox account to avoid polluting test data?
- Token tracking: should we instrument the hypothesis LLM calls to track autoresearch's own cost?
