"""Tests for the vs-MCP per-case latency gate (#2).

The gate flags any case whose live latency exceeds its per-case budget
(Case.max_latency_ms). NOT success-gated, so it catches timeouts — the founding
2026-06 ship-to-country incident was a timeout (a failure), which a success-gated
check would have missed.
"""

from dataclasses import dataclass

from app.services.benchmarks.run_vs_mcp import latency_breach


@dataclass
class _Side:
    latency_ms: int
    success: bool = True


class TestLatencyBreach:
    def test_succeeded_over_budget_is_breach(self):
        assert latency_breach(_Side(latency_ms=90_000, success=True), 60_000) is True

    def test_succeeded_under_budget_is_not_breach(self):
        assert latency_breach(_Side(latency_ms=30_000, success=True), 60_000) is False

    def test_exactly_at_budget_is_not_breach(self):
        assert latency_breach(_Side(latency_ms=60_000, success=True), 60_000) is False

    def test_timeout_failure_over_budget_is_breach(self):
        # The founding incident: a TIMEOUT (success=False, latency ~= 180s cap) MUST flag.
        assert latency_breach(_Side(latency_ms=180_000, success=False), 60_000) is True

    def test_fast_failure_under_budget_is_not_breach(self):
        # Connection error fails fast -> not a slowness signal.
        assert latency_breach(_Side(latency_ms=500, success=False), 60_000) is False

    def test_none_side_is_not_breach(self):
        assert latency_breach(None, 60_000) is False

    def test_zero_budget_is_not_breach(self):
        assert latency_breach(_Side(latency_ms=999_999, success=True), 0) is False


# Synthetic stand-ins for SideScore / Case / CaseResult, reused across the
# collector + CLI tests (the real ones carry many more fields than the gate reads).
@dataclass
class _FullSide:
    latency_ms: int
    success: bool = True
    answer_acc: float = 1.0
    tool_acc: float = 1.0
    cost_usd: float = 0.0
    error: str | None = None
    answer_preview: str = ""


@dataclass
class _Case:
    case_id: str
    max_latency_ms: int


@dataclass
class _Result:
    case: "_Case"
    ours: "_FullSide"
    mcp: "_FullSide | None" = None
    verdict: str = "OURS ONLY"

    def delta_accuracy(self) -> float:
        return 0.0


class TestCollectLatencyBreaches:
    def test_collects_only_breaching_cases(self):
        from app.services.benchmarks.run_vs_mcp import collect_latency_breaches

        results = [
            _Result(_Case("fast", 60_000), _FullSide(latency_ms=10_000)),
            _Result(_Case("slow", 60_000), _FullSide(latency_ms=90_000)),
        ]
        breaches = collect_latency_breaches(results)
        assert [b.case_id for b in breaches] == ["slow"]
        assert breaches[0].ours_latency_ms == 90_000
        assert breaches[0].budget_ms == 60_000

    def test_includes_mcp_latency_and_ratio_for_triage(self):
        from app.services.benchmarks.run_vs_mcp import collect_latency_breaches

        results = [
            _Result(
                _Case("slow", 60_000),
                _FullSide(latency_ms=90_000),
                _FullSide(latency_ms=45_000),
            )
        ]
        b = collect_latency_breaches(results)[0]
        assert b.mcp_latency_ms == 45_000
        assert b.ours_over_mcp_ratio == 2.0  # 90000 / 45000

    def test_no_mcp_leaves_ratio_none(self):
        from app.services.benchmarks.run_vs_mcp import collect_latency_breaches

        results = [_Result(_Case("slow", 60_000), _FullSide(latency_ms=90_000), None)]
        b = collect_latency_breaches(results)[0]
        assert b.mcp_latency_ms is None
        assert b.ours_over_mcp_ratio is None

    def test_empty_results(self):
        from app.services.benchmarks.run_vs_mcp import collect_latency_breaches

        assert collect_latency_breaches([]) == []
