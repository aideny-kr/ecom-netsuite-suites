"""Tests for the vs-MCP per-case latency monitor (#2).

ADVISORY by design: this is a nightly monitor (Sentry/log/email alert + CLI report),
NOT a CI gate — it never fails CI or changes a verdict. Latency is noisy, and a true
timeout already fails CI independently (success=False → OURS FAILED). It flags any case
whose live latency exceeds its per-case budget (Case.max_latency_ms), NOT success-gated
so it catches timeouts — the founding 2026-06 ship-to-country incident was a timeout
(a failure), which a success-gated check would have missed.
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


class TestCliReport:
    def test_print_summary_reports_latency_breaches(self):
        import io
        from contextlib import redirect_stdout

        from app.services.benchmarks.run_vs_mcp import _print_summary

        results = [
            _Result(_Case("slow", 60_000), _FullSide(latency_ms=92_000)),
            _Result(_Case("ok", 60_000), _FullSide(latency_ms=10_000)),
        ]
        buf = io.StringIO()
        with redirect_stdout(buf):
            _print_summary(results, skip_baseline=True)
        out = buf.getvalue()
        assert "Latency budget breaches (advisory): 1" in out
        assert "slow" in out

    def test_print_summary_no_breaches_silent(self):
        import io
        from contextlib import redirect_stdout

        from app.services.benchmarks.run_vs_mcp import _print_summary

        results = [_Result(_Case("ok", 60_000), _FullSide(latency_ms=10_000))]
        buf = io.StringIO()
        with redirect_stdout(buf):
            _print_summary(results, skip_baseline=True)
        assert "Latency budget breaches" not in buf.getvalue()


class TestEmitLatencyAlert:
    def test_logs_and_captures_sentry(self):
        import sys
        import uuid
        from unittest.mock import MagicMock, patch

        import app.workers.tasks.agent_benchmark_vs_mcp as task_mod
        from app.services.benchmarks.run_vs_mcp import LatencyBreach

        breaching = [LatencyBreach("slow", 92_000, 60_000, 45_000, 2.04)]
        fake_sentry = MagicMock()
        with (
            patch.object(task_mod, "logger") as mock_logger,
            patch.dict(sys.modules, {"sentry_sdk": fake_sentry}),
        ):
            task_mod._emit_latency_alert(
                tenant_id=uuid.UUID("ce3dfaad-626f-4992-84e9-500c8291ca0a"),
                breaching=breaching,
            )
        # structured ERROR log on the latency channel
        mock_logger.error.assert_called_once()
        assert mock_logger.error.call_args.args[0] == "agent_benchmark.latency_regression_detected"
        # Sentry best-effort capture fired
        fake_sentry.capture_message.assert_called_once()
        assert fake_sentry.capture_message.call_args.kwargs.get("level") == "error"


class TestApplyLatencyStats:
    def test_sets_flags_and_alerts_on_breach(self):
        import uuid
        from unittest.mock import patch

        from app.workers.tasks.agent_benchmark_vs_mcp import _apply_latency_stats

        results = [
            _Result(_Case("slow", 60_000), _FullSide(latency_ms=92_000)),
            _Result(_Case("ok", 60_000), _FullSide(latency_ms=10_000)),
        ]
        stats: dict = {}
        with patch("app.workers.tasks.agent_benchmark_vs_mcp._emit_latency_alert") as mock_alert:
            _apply_latency_stats(stats=stats, results=results, tenant_id=uuid.uuid4())
        assert stats["latency_breaches"] == 1
        assert stats["latency_regression_detected"] is True
        assert stats["latency_breach_cases"] == ["slow"]
        mock_alert.assert_called_once()

    def test_no_breach_no_alert(self):
        import uuid
        from unittest.mock import patch

        from app.workers.tasks.agent_benchmark_vs_mcp import _apply_latency_stats

        results = [_Result(_Case("ok", 60_000), _FullSide(latency_ms=10_000))]
        stats: dict = {}
        with patch("app.workers.tasks.agent_benchmark_vs_mcp._emit_latency_alert") as mock_alert:
            _apply_latency_stats(stats=stats, results=results, tenant_id=uuid.uuid4())
        assert stats["latency_breaches"] == 0
        assert stats["latency_regression_detected"] is False
        mock_alert.assert_not_called()

    def test_alert_failure_is_swallowed(self):
        # An alert/Sentry failure must never sink the nightly run after its work is done
        # (grill diff Finding 2). stats are recorded regardless.
        import uuid
        from unittest.mock import patch

        from app.workers.tasks.agent_benchmark_vs_mcp import _apply_latency_stats

        results = [_Result(_Case("slow", 60_000), _FullSide(latency_ms=92_000))]
        stats: dict = {}
        with patch(
            "app.workers.tasks.agent_benchmark_vs_mcp._emit_latency_alert",
            side_effect=RuntimeError("sentry down"),
        ):
            _apply_latency_stats(stats=stats, results=results, tenant_id=uuid.uuid4())  # must not raise
        assert stats["latency_breaches"] == 1


class TestDigestRender:
    def test_html_renders_latency_breaches(self):
        from datetime import date

        from app.services.benchmark_email_service import _build_html_body

        stats = {
            "ours_wins": 5,
            "mcp_wins": 0,
            "ties": 0,
            "failures": 0,
            "cases_run": 5,
            "avg_delta_accuracy": 0.1,
            "latency_breaches": 1,
            "latency_breach_cases": ["sales_country_canonical"],
        }
        html = _build_html_body(run_date=date(2026, 6, 5), stats=stats, regression_detected=False)
        assert "Latency budget breach" in html
        assert "sales_country_canonical" in html

    def test_html_no_latency_block_when_clean(self):
        from datetime import date

        from app.services.benchmark_email_service import _build_html_body

        stats = {"ours_wins": 5, "mcp_wins": 0, "ties": 0, "cases_run": 5, "avg_delta_accuracy": 0.1}
        html = _build_html_body(run_date=date(2026, 6, 5), stats=stats, regression_detected=False)
        assert "Latency budget breach" not in html

    def test_subject_flags_latency_breach(self):
        # A latency-only breach (accuracy fine) must be visible in the subject, or it's
        # buried in the body and missed (multi-angle review Finding 7).
        from datetime import date

        from app.services.benchmark_email_service import _build_subject

        stats = {
            "ours_wins": 5,
            "mcp_wins": 0,
            "ties": 0,
            "cases_run": 5,
            "avg_delta_accuracy": 0.1,
            "latency_breaches": 2,
        }
        subj = _build_subject(run_date=date(2026, 6, 7), stats=stats, regression_detected=False)
        assert "latency" in subj.lower()
        assert "2" in subj

    def test_subject_clean_when_no_latency_breach(self):
        from datetime import date

        from app.services.benchmark_email_service import _build_subject

        stats = {"ours_wins": 5, "mcp_wins": 0, "ties": 0, "cases_run": 5, "avg_delta_accuracy": 0.1}
        subj = _build_subject(run_date=date(2026, 6, 7), stats=stats, regression_detected=False)
        assert "latency" not in subj.lower()
