"""CLI benchmark runner: our agent vs Claude + Oracle NetSuite MCP.

The single command that ends manual agent testing. Loads a set of YAML
benchmark cases, runs each one through BOTH our in-house UnifiedAgent AND
a minimal Claude-with-MCP baseline, compares the results side-by-side,
and prints a verdict table.

Usage
-----

Quick smoke test — single canonical case:

    cd backend
    .venv/bin/python -m tests.agent_benchmarks.run_vs_mcp \\
        --case country_sales_canonical \\
        --tenant-id ce3dfaad-626f-4992-84e9-500c8291ca0a

All country-sales variations (6 cases total — canonical + 5 variations):

    .venv/bin/python -m tests.agent_benchmarks.run_vs_mcp \\
        --suite country_sales \\
        --tenant-id ce3dfaad-626f-4992-84e9-500c8291ca0a

Compare against Opus-class baseline ("can we beat our best ceiling?"):

    .venv/bin/python -m tests.agent_benchmarks.run_vs_mcp \\
        --suite country_sales \\
        --tenant-id ce3dfaad-626f-4992-84e9-500c8291ca0a \\
        --baseline-model claude-opus-4-6

Skip the baseline (just time-and-score our agent):

    .venv/bin/python -m tests.agent_benchmarks.run_vs_mcp \\
        --suite country_sales \\
        --tenant-id ce3dfaad-626f-4992-84e9-500c8291ca0a \\
        --skip-baseline

Output
------

For each case, a line showing accuracy, tool-selection correctness, cost,
and latency on BOTH sides, plus a verdict:

    ┌──────────────────────────┬──────┬──────┬──────┬──────────┐
    │ Case                     │ Ours │ MCP  │ Δacc │ Verdict  │
    ├──────────────────────────┼──────┼──────┼──────┼──────────┤
    │ country_sales_canonical  │ 1.00 │ 1.00 │ 0.00 │ TIE      │
    │ country_sales_variation_1│ 0.50 │ 1.00 │-0.50 │ MCP WINS │
    │ country_sales_variation_3│ 0.75 │ 1.00 │-0.25 │ MCP WINS │
    └──────────────────────────┴──────┴──────┴──────┴──────────┘

    SUMMARY
    Ours avg accuracy: 0.75  |  avg cost: $0.18  |  avg latency: 14.2s
    MCP  avg accuracy: 1.00  |  avg cost: $0.09  |  avg latency:  8.1s
    Wins: Ours 0  MCP 2  Tie 1
    NORTH STAR: MCP + Claude is currently beating us 2-0-1.

Exit codes
----------
0 = our agent matched or beat the baseline on every case
1 = our agent lost on at least one case (regression or ongoing gap)
2 = harness error (could not run one or more cases)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import mean
from typing import Any

import yaml

# These imports are intentionally kept lazy inside main() to avoid
# requiring the full app stack when the user runs --help.


_CASE_DIR = Path(__file__).parent / "benchmark_cases" / "vs_mcp"


# ---------------------------------------------------------------------------
# Case loading + scoring
# ---------------------------------------------------------------------------


@dataclass
class Case:
    case_id: str
    query: str
    expected_answer_contains: list[str]
    expected_tools: list[str]
    expected_accuracy: float
    max_cost: float
    max_latency_ms: int
    tags: list[str]
    notes: str
    baseline_expected_tools: list[str]
    baseline_expected_accuracy: float


def _load_case_file(path: Path) -> Case:
    data = yaml.safe_load(path.read_text()) or {}
    return Case(
        case_id=data.get("case_id") or path.stem,
        query=data["query"],
        expected_answer_contains=data.get("expected_answer_contains", []),
        expected_tools=data.get("expected_tools", []),
        expected_accuracy=float(data.get("expected_accuracy", 0.7)),
        max_cost=float(data.get("max_cost", 0.50)),
        max_latency_ms=int(data.get("max_latency_ms", 120_000)),
        tags=list(data.get("tags", [])),
        notes=str(data.get("notes", "")),
        baseline_expected_tools=data.get("baseline_expected_tools", []),
        baseline_expected_accuracy=float(data.get("baseline_expected_accuracy", 0.7)),
    )


def load_cases(
    *,
    case_ids: list[str] | None = None,
    suite: str | None = None,
) -> list[Case]:
    """Load benchmark cases from the vs_mcp directory.

    Args:
        case_ids: Specific case IDs to load. If None, load all cases.
        suite: Optional name prefix — e.g. "country_sales" loads all files
            starting with country_sales_*.
    """
    if not _CASE_DIR.is_dir():
        raise FileNotFoundError(f"Benchmark case directory not found: {_CASE_DIR}")

    all_files = sorted(_CASE_DIR.glob("*.yaml"))
    if not all_files:
        raise FileNotFoundError(f"No YAML cases in {_CASE_DIR}")

    cases = [_load_case_file(p) for p in all_files]

    if case_ids:
        wanted = set(case_ids)
        cases = [c for c in cases if c.case_id in wanted]
        missing = wanted - {c.case_id for c in cases}
        if missing:
            raise ValueError(f"Unknown case IDs: {sorted(missing)}")

    if suite:
        cases = [c for c in cases if c.case_id.startswith(suite)]
        if not cases:
            raise ValueError(f"No cases match suite prefix '{suite}'")

    return cases


async def _score_answer(
    *,
    question: str,
    answer_text: str,
    expected_contains: list[str],
    use_llm_judge: bool,
) -> tuple[float, str]:
    """Score an agent answer. Returns (score, rationale).

    When `use_llm_judge` is True, uses Claude Haiku as evaluator — catches
    "I couldn't find" / hallucinated zero results that substring matching
    falsely credits.

    When False (or on judge error), falls back to substring scoring with
    failure-phrase penalty.
    """
    from tests.agent_benchmarks.scorer import llm_judge_score, substring_score

    if use_llm_judge:
        result = await llm_judge_score(
            question=question,
            answer_text=answer_text,
            expected_contains=expected_contains,
        )
    else:
        result = substring_score(
            answer_text=answer_text,
            expected_contains=expected_contains,
        )
    return result.score, f"[{result.source}] {result.rationale}"


def _score_tools(
    *,
    tool_calls: list[dict],
    expected_tools: list[str],
) -> float:
    """Fraction of expected tools that actually got called. Superset OK.

    Matches on tool name substring so `ext__<hex>__ns_runCustomSuiteQL`
    matches an expected `ns_runCustomSuiteQL` entry.
    """
    if not expected_tools:
        return 1.0
    used_names = [str(tc.get("name") or tc.get("tool") or "") for tc in (tool_calls or [])]
    hits = 0
    for expected in expected_tools:
        if any(expected in used for used in used_names):
            hits += 1
    return hits / len(expected_tools)


# ---------------------------------------------------------------------------
# Runner + verdict computation
# ---------------------------------------------------------------------------


@dataclass
class SideScore:
    answer_acc: float
    tool_acc: float
    cost_usd: float
    latency_ms: int
    success: bool
    error: str | None
    answer_preview: str


@dataclass
class CaseResult:
    case: Case
    ours: SideScore
    mcp: SideScore | None  # None when --skip-baseline
    verdict: str  # "OURS WINS" | "MCP WINS" | "TIE" | "BOTH FAILED" | "OURS ONLY"
    # Raw result objects — kept so persistence can access token counts,
    # context_chars, tool_calls, confidence_score, etc. without re-running.
    ours_raw: Any = None  # AgentRunResult | None
    mcp_raw: Any = None  # BaselineResult | None

    def delta_accuracy(self) -> float:
        if self.mcp is None:
            return 0.0
        return round(self.ours.answer_acc - self.mcp.answer_acc, 3)


def _compute_verdict(ours: SideScore, mcp: SideScore | None) -> str:
    if mcp is None:
        if not ours.success:
            return "OURS FAILED"
        return "OURS ONLY"
    if not ours.success and not mcp.success:
        return "BOTH FAILED"
    if not ours.success:
        return "MCP WINS"
    if not mcp.success:
        return "OURS WINS"
    delta = ours.answer_acc - mcp.answer_acc
    if delta > 0.05:
        return "OURS WINS"
    if delta < -0.05:
        return "MCP WINS"
    return "TIE"


async def _run_single_case(
    *,
    case: Case,
    tenant_id: uuid.UUID,
    agent_model: str,
    baseline_model: str,
    skip_baseline: bool,
    use_llm_judge: bool,
    db,
) -> CaseResult:
    from tests.agent_benchmarks.agent_runner import run_agent
    from tests.agent_benchmarks.baseline_runner import run_baseline

    # Run ours
    agent_result = None
    t0 = time.monotonic()
    try:
        agent_result = await run_agent(
            tenant_id=tenant_id,
            question=case.query,
            db=db,
            model=agent_model,
        )
        agent_latency_ms = int((time.monotonic() - t0) * 1000)
    except Exception as exc:  # pragma: no cover - safety net
        agent_latency_ms = int((time.monotonic() - t0) * 1000)
        ours_side = SideScore(
            answer_acc=0.0,
            tool_acc=0.0,
            cost_usd=0.0,
            latency_ms=agent_latency_ms,
            success=False,
            error=f"agent_runner crash: {exc}",
            answer_preview="",
        )
    else:
        ours_acc, ours_rationale = await _score_answer(
            question=case.query,
            answer_text=agent_result.answer_text,
            expected_contains=case.expected_answer_contains,
            use_llm_judge=use_llm_judge,
        )
        ours_side = SideScore(
            answer_acc=ours_acc,
            tool_acc=_score_tools(
                tool_calls=agent_result.tool_calls,
                expected_tools=case.expected_tools,
            ),
            cost_usd=agent_result.cost_usd,
            latency_ms=agent_result.latency_ms or agent_latency_ms,
            success=agent_result.success,
            error=agent_result.error,
            answer_preview=(agent_result.answer_text or "")[:240],
        )
        print(f"    ours score rationale: {ours_rationale}")

    # Run baseline
    if skip_baseline:
        return CaseResult(
            case=case,
            ours=ours_side,
            mcp=None,
            verdict=_compute_verdict(ours_side, None),
            ours_raw=agent_result,
            mcp_raw=None,
        )

    baseline_result = None
    t0 = time.monotonic()
    try:
        baseline_result = await run_baseline(
            tenant_id=tenant_id,
            question=case.query,
            model=baseline_model,
            db=db,
        )
        baseline_latency_ms = int((time.monotonic() - t0) * 1000)
    except Exception as exc:  # pragma: no cover - safety net
        baseline_latency_ms = int((time.monotonic() - t0) * 1000)
        mcp_side = SideScore(
            answer_acc=0.0,
            tool_acc=0.0,
            cost_usd=0.0,
            latency_ms=baseline_latency_ms,
            success=False,
            error=f"baseline_runner crash: {exc}",
            answer_preview="",
        )
    else:
        mcp_acc, mcp_rationale = await _score_answer(
            question=case.query,
            answer_text=baseline_result.answer_text,
            expected_contains=case.expected_answer_contains,
            use_llm_judge=use_llm_judge,
        )
        mcp_side = SideScore(
            answer_acc=mcp_acc,
            tool_acc=_score_tools(
                tool_calls=baseline_result.tool_calls,
                expected_tools=case.baseline_expected_tools or case.expected_tools,
            ),
            cost_usd=baseline_result.cost_usd,
            latency_ms=baseline_result.latency_ms or baseline_latency_ms,
            success=baseline_result.success,
            error=baseline_result.error,
            answer_preview=(baseline_result.answer_text or "")[:240],
        )
        print(f"    mcp  score rationale: {mcp_rationale}")

    return CaseResult(
        case=case,
        ours=ours_side,
        mcp=mcp_side,
        verdict=_compute_verdict(ours_side, mcp_side),
        ours_raw=agent_result,
        mcp_raw=baseline_result,
    )


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _format_cost(n: float) -> str:
    return f"${n:.3f}"


def _format_ms(ms: int) -> str:
    if ms >= 10_000:
        return f"{ms / 1000:.1f}s"
    return f"{ms}ms"


def _print_results_table(results: list[CaseResult], skip_baseline: bool) -> None:
    header_case = "Case"
    if skip_baseline:
        cols = [header_case, "Ours acc", "Ours tool", "Cost", "Latency", "Verdict"]
    else:
        cols = [header_case, "Ours", "MCP", "Δacc", "Cost O/M", "Lat O/M", "Verdict"]

    rows: list[list[str]] = []
    for r in results:
        if skip_baseline:
            rows.append([
                r.case.case_id,
                f"{r.ours.answer_acc:.2f}",
                f"{r.ours.tool_acc:.2f}",
                _format_cost(r.ours.cost_usd),
                _format_ms(r.ours.latency_ms),
                r.verdict,
            ])
        else:
            assert r.mcp is not None
            rows.append([
                r.case.case_id,
                f"{r.ours.answer_acc:.2f}",
                f"{r.mcp.answer_acc:.2f}",
                f"{r.delta_accuracy():+.2f}",
                f"{_format_cost(r.ours.cost_usd)}/{_format_cost(r.mcp.cost_usd)}",
                f"{_format_ms(r.ours.latency_ms)}/{_format_ms(r.mcp.latency_ms)}",
                r.verdict,
            ])

    # Column widths
    widths = [max(len(str(row[i])) for row in ([cols] + rows)) for i in range(len(cols))]

    def fmt_row(row: list[str]) -> str:
        return " | ".join(str(v).ljust(w) for v, w in zip(row, widths))

    sep = "-+-".join("-" * w for w in widths)
    print()
    print(fmt_row(cols))
    print(sep)
    for row in rows:
        print(fmt_row(row))
    print()


def _print_summary(results: list[CaseResult], skip_baseline: bool) -> None:
    total = len(results)
    if total == 0:
        print("No results to summarize.")
        return

    ours_acc = mean(r.ours.answer_acc for r in results)
    ours_cost = mean(r.ours.cost_usd for r in results)
    ours_latency = mean(r.ours.latency_ms for r in results)

    print("SUMMARY")
    print(f"  Ours avg accuracy: {ours_acc:.2f}  |  avg cost: {_format_cost(ours_cost)}"
          f"  |  avg latency: {_format_ms(int(ours_latency))}")

    if skip_baseline:
        errors = [r for r in results if not r.ours.success]
        if errors:
            print(f"  Failed cases: {len(errors)}/{total}")
            for r in errors:
                print(f"    ! {r.case.case_id}: {r.ours.error}")
        return

    mcp_acc = mean(r.mcp.answer_acc for r in results if r.mcp is not None)
    mcp_cost = mean(r.mcp.cost_usd for r in results if r.mcp is not None)
    mcp_latency = mean(r.mcp.latency_ms for r in results if r.mcp is not None)

    print(f"  MCP  avg accuracy: {mcp_acc:.2f}  |  avg cost: {_format_cost(mcp_cost)}"
          f"  |  avg latency: {_format_ms(int(mcp_latency))}")

    wins = sum(1 for r in results if r.verdict == "OURS WINS")
    losses = sum(1 for r in results if r.verdict == "MCP WINS")
    ties = sum(1 for r in results if r.verdict == "TIE")

    print(f"  Wins:  Ours {wins}  |  MCP {losses}  |  Tie {ties}")
    print()
    if losses > 0:
        print(f"  NORTH STAR STATUS: MCP + Claude is beating us on {losses}/{total} cases.")
        print("  Regression cases:")
        for r in results:
            if r.verdict == "MCP WINS":
                print(f"    - {r.case.case_id}: Δacc={r.delta_accuracy():+.2f}"
                      f" | ours='{r.ours.answer_preview[:80]}...'"
                      f" | mcp='{(r.mcp.answer_preview if r.mcp else '')[:80]}...'")
    elif ours_acc >= mcp_acc:
        print(f"  NORTH STAR STATUS: OURS matches or beats MCP on all {total} cases.")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def _main_async(args: argparse.Namespace) -> int:
    try:
        cases = load_cases(case_ids=args.case, suite=args.suite)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error loading cases: {exc}", file=sys.stderr)
        return 2

    if not cases:
        print("No cases selected. Use --case or --suite.", file=sys.stderr)
        return 2

    print(f"Running {len(cases)} case(s) against tenant {args.tenant_id}")
    print(f"Agent model:    {args.agent_model}")
    if not args.skip_baseline:
        print(f"Baseline model: {args.baseline_model}")
    else:
        print("Baseline:       SKIPPED (--skip-baseline)")
    print()

    # Lazy DB import
    from app.core.database import async_session_factory, set_tenant_context

    tenant_uuid = uuid.UUID(args.tenant_id)
    run_id = uuid.uuid4()
    run_date = date.today()

    if args.persist:
        print(f"Persistence:    ENABLED (run_id={run_id})")
    print()

    results: list[CaseResult] = []
    async with async_session_factory() as db:
        await set_tenant_context(db, str(tenant_uuid))

        for i, case in enumerate(cases, 1):
            print(f"[{i}/{len(cases)}] {case.case_id}: {case.query[:80]}")
            result = await _run_single_case(
                case=case,
                tenant_id=tenant_uuid,
                agent_model=args.agent_model,
                baseline_model=args.baseline_model,
                skip_baseline=args.skip_baseline,
                use_llm_judge=not args.no_llm_judge,
                db=db,
            )
            results.append(result)
            print(f"  → verdict: {result.verdict}")
            if result.ours.error:
                print(f"    ours error: {result.ours.error}")
            if result.mcp and result.mcp.error:
                print(f"    mcp error:  {result.mcp.error}")

            # Persist both sides if --persist
            if args.persist and result.ours_raw is not None:
                from tests.agent_benchmarks.persistence import persist_case_result

                try:
                    await persist_case_result(
                        db=db,
                        tenant_id=tenant_uuid,
                        run_id=run_id,
                        run_date=run_date,
                        case_id=case.case_id,
                        side="ours",
                        model=args.agent_model,
                        result=result.ours_raw,
                        answer_accuracy=result.ours.answer_acc,
                        tool_accuracy=result.ours.tool_acc,
                    )
                    if result.mcp_raw is not None:
                        await persist_case_result(
                            db=db,
                            tenant_id=tenant_uuid,
                            run_id=run_id,
                            run_date=run_date,
                            case_id=case.case_id,
                            side="mcp",
                            model=args.baseline_model,
                            result=result.mcp_raw,
                            answer_accuracy=result.mcp.answer_acc if result.mcp else 0.0,
                            tool_accuracy=result.mcp.tool_acc if result.mcp else 0.0,
                        )
                    await db.commit()
                except Exception as exc:
                    print(f"    ⚠ persistence failed: {exc}", file=sys.stderr)
                    await db.rollback()

    _print_results_table(results, args.skip_baseline)
    _print_summary(results, args.skip_baseline)

    if args.persist:
        print(f"Persisted to agent_benchmark_runs (run_id={run_id})")
        print()

    # Exit code: 1 if any case was lost (or "OURS FAILED")
    has_loss = any(r.verdict in ("MCP WINS", "OURS FAILED", "BOTH FAILED") for r in results)
    return 1 if has_loss else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run benchmark cases through our agent AND the Claude+MCP baseline, "
                    "and print a side-by-side comparison.",
    )
    parser.add_argument(
        "--tenant-id",
        required=True,
        help="Tenant UUID to run the benchmark against (e.g. Framework: "
             "ce3dfaad-626f-4992-84e9-500c8291ca0a)",
    )
    parser.add_argument(
        "--case",
        action="append",
        default=None,
        help="Run a specific case by ID. Repeatable. Mutually exclusive with --suite.",
    )
    parser.add_argument(
        "--suite",
        default=None,
        help="Run all cases whose ID starts with this prefix (e.g. 'country_sales').",
    )
    parser.add_argument(
        "--agent-model",
        default="claude-sonnet-4-6",
        help="Model for our in-house agent. Default: claude-sonnet-4-6.",
    )
    parser.add_argument(
        "--baseline-model",
        default="claude-sonnet-4-6",
        help="Model for the Claude+MCP baseline. Default: claude-sonnet-4-6 "
             "(apples-to-apples with the agent).",
    )
    parser.add_argument(
        "--skip-baseline",
        action="store_true",
        help="Skip the Claude+MCP baseline and just time-and-score our agent.",
    )
    parser.add_argument(
        "--persist",
        action="store_true",
        help="Write each case result to the agent_benchmark_runs table. "
             "Enabled by default for the nightly cron; use it manually if "
             "you want the run to become the new historical baseline.",
    )
    parser.add_argument(
        "--no-llm-judge",
        action="store_true",
        help="Skip the LLM-judge (Claude Haiku) and fall back to substring "
             "scoring with failure-phrase penalty. Faster and free but "
             "scores a 'I couldn't find Norway' answer as correct when "
             "Norway was expected. Default: judge enabled.",
    )

    args = parser.parse_args()

    if args.case is None and args.suite is None:
        # Default to all vs_mcp cases if nothing specified
        pass

    try:
        exit_code = asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
