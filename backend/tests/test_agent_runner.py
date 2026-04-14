"""Unit tests for the in-house UnifiedAgent benchmark runner.

TDD: these tests are written BEFORE the implementation. They mock every
external dependency so the tests never touch the real DB, Anthropic API,
filesystem, or tenant config.

The contract under test is the public surface exposed by
``app.services.benchmarks.agent_runner``:

* :class:`AgentRunResult` dataclass (field-compatible with ``BaselineResult``
  plus three extras used by the benchmark harness).
* :func:`run_agent` async entry point.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.chat.agents.base_agent import AgentResult
from app.services.chat.llm_adapter import TokenUsage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_TENANT_ID = uuid.UUID("ce3dfaad-626f-4992-84e9-500c8291ca0a")


def _make_agent_result(
    *,
    data: str = "The answer is 42.",
    tool_calls_log: list[dict] | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation: int = 0,
    cache_read: int = 0,
    success: bool = True,
    confidence: float | None = 4.5,
) -> AgentResult:
    return AgentResult(
        success=success,
        data=data,
        tool_calls_log=tool_calls_log or [],
        tokens_used=TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=cache_creation,
            cache_read_input_tokens=cache_read,
        ),
        agent_name="unified",
        confidence_score=confidence,
    )


class _StubUnifiedAgent:
    """Minimal stand-in for UnifiedAgent that records how it was configured.

    Mirrors the real agent's public field surface enough for run_agent to
    read `system_prompt` after setup and drive `run_streaming` to yield a
    canned AgentResult.
    """

    def __init__(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        correlation_id: str,
        metadata=None,
        policy=None,
        context_need: str = "FULL",
    ) -> None:
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.correlation_id = correlation_id
        self._metadata = metadata
        self._policy = policy
        self._context_need = context_need
        self._tool_defs: list[dict] | None = None
        self._tenant_vernacular: str = ""
        self._onboarding_profile: str = ""
        self._soul_quirks: str = ""
        self._soul_tone: str = ""
        self._brand_name: str = ""
        self._netsuite_account_slug: str = ""
        self._user_timezone: str | None = None
        self._current_task: str = ""
        self._domain_knowledge: list[str] = []
        self._proven_patterns: list[dict] = []
        self._active_skill = None
        self._context: dict = {}
        self._connectors: list = []

        # Canned response — tests override before calling run_streaming.
        self._canned_result: AgentResult = _make_agent_result()
        self._canned_events: list[tuple[str, object]] = []
        self._raise_on_run: Exception | None = None

    @property
    def system_prompt(self) -> str:
        # Minimal non-empty prompt so `context_chars` ends up > 0.
        return (
            f"SYSTEM PROMPT FOR {self._brand_name or 'tenant'} "
            f"vernacular={self._tenant_vernacular} "
            f"dk_chunks={len(self._domain_knowledge)} "
            f"patterns={len(self._proven_patterns)}"
        )

    async def run_streaming(self, *args, **kwargs):
        if self._raise_on_run is not None:
            raise self._raise_on_run
        for event in self._canned_events:
            yield event
        yield "response", self._canned_result


@pytest.fixture
def stub_agent_cls():
    return _StubUnifiedAgent


@pytest.fixture
def db_mock():
    return MagicMock()


@pytest.fixture
def patched_deps(stub_agent_cls):
    """Patch all the orchestrator context-loading helpers so run_agent never
    touches the DB or the real UnifiedAgent."""
    # CI doesn't set ANTHROPIC_API_KEY; patch it so run_agent's early-exit
    # guard (missing_anthropic_api_key) doesn't short-circuit the test.
    with (
        patch("app.core.config.settings.ANTHROPIC_API_KEY", "sk-test-not-real"),
        patch(
            "app.services.benchmarks.agent_runner.UnifiedAgent",
            stub_agent_cls,
        ),
        patch(
            "app.services.benchmarks.agent_runner.get_active_metadata",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.services.benchmarks.agent_runner.get_active_connectors_for_tenant",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.services.benchmarks.agent_runner.retrieve_domain_knowledge",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.services.benchmarks.agent_runner.retrieve_similar_patterns",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.services.benchmarks.agent_runner.retrieve_learned_rules",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.services.benchmarks.agent_runner.TenantEntityResolver.resolve_entities",
            new=AsyncMock(return_value=""),
        ),
        patch(
            "app.services.benchmarks.agent_runner.build_all_tool_definitions",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.services.benchmarks.agent_runner._build_adapter",
            return_value=MagicMock(),
        ),
        patch(
            "app.services.benchmarks.agent_runner._load_tenant_config",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    brand_name="TestCo",
                    fiscal_year_start_month=1,
                )
            ),
        ),
    ):
        yield


# ---------------------------------------------------------------------------
# Dataclass contract
# ---------------------------------------------------------------------------


class TestAgentRunResult:
    def test_has_baseline_compatible_fields(self):
        from app.services.benchmarks.agent_runner import AgentRunResult

        result = AgentRunResult(
            answer_text="hi",
            tool_calls=[],
            input_tokens=1,
            output_tokens=2,
            cost_usd=0.0,
            latency_ms=10,
            success=True,
            error=None,
            confidence_score=3.5,
            num_steps=0,
            context_chars=123,
        )
        assert result.answer_text == "hi"
        assert result.tool_calls == []
        assert result.input_tokens == 1
        assert result.output_tokens == 2
        assert result.cost_usd == 0.0
        assert result.latency_ms == 10
        assert result.success is True
        assert result.error is None
        assert result.confidence_score == 3.5
        assert result.num_steps == 0
        assert result.context_chars == 123


# ---------------------------------------------------------------------------
# Cost calculation
# ---------------------------------------------------------------------------


class TestCostCalculation:
    def test_sonnet_pricing_table(self):
        # 10K input + 5K output on sonnet: 0.010 * 3 + 0.005 * 15 = $0.105
        from app.services.benchmarks.agent_runner import _calculate_cost

        cost = _calculate_cost(
            model="claude-sonnet-4-6",
            input_tokens=10_000,
            output_tokens=5_000,
        )
        assert cost == pytest.approx(0.105)

    def test_opus_pricing_table(self):
        # 1K input + 1K output on opus: 0.001 * 15 + 0.001 * 75 = $0.090
        from app.services.benchmarks.agent_runner import _calculate_cost

        cost = _calculate_cost(
            model="claude-opus-4-6",
            input_tokens=1_000,
            output_tokens=1_000,
        )
        assert cost == pytest.approx(0.090)

    def test_unknown_model_falls_back_to_sonnet(self):
        from app.services.benchmarks.agent_runner import _calculate_cost

        cost = _calculate_cost(
            model="not-a-real-model",
            input_tokens=1_000_000,
            output_tokens=0,
        )
        # Sonnet input rate = $3/MTok → $3 for 1M tokens
        assert cost == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRunAgentHappyPath:
    async def test_text_only_response_produces_successful_result(
        self,
        patched_deps,
        db_mock,
    ):
        from app.services.benchmarks import agent_runner

        canned = _make_agent_result(
            data="There were 42 orders last week.",
            tool_calls_log=[],
            input_tokens=5_000,
            output_tokens=200,
            confidence=4.0,
        )

        # Swap the default canned response on the stub class so the next
        # instance returns our fixture data.
        original_init = agent_runner.UnifiedAgent.__init__

        def _init_with_canned(self, *a, **kw):
            original_init(self, *a, **kw)
            self._canned_result = canned

        with patch.object(agent_runner.UnifiedAgent, "__init__", _init_with_canned):
            result = await agent_runner.run_agent(
                tenant_id=_TENANT_ID,
                question="How many orders last week?",
                db=db_mock,
            )

        assert result.success is True
        assert result.error is None
        assert "42 orders" in result.answer_text
        assert result.tool_calls == []
        assert result.num_steps == 0
        assert result.input_tokens == 5_000
        assert result.output_tokens == 200
        # 0.005 * 3 + 0.0002 * 15 = 0.015 + 0.003 = 0.018
        assert result.cost_usd == pytest.approx(0.018)
        assert result.confidence_score == pytest.approx(4.0)
        assert result.latency_ms >= 0
        assert result.context_chars > 0


@pytest.mark.asyncio
class TestRunAgentToolUsePath:
    async def test_single_tool_call_logged(self, patched_deps, db_mock):
        from app.services.benchmarks import agent_runner

        tool_log_entry = {
            "step": 0,
            "tool": "netsuite_suiteql",
            "params": {"query": "SELECT * FROM transaction FETCH FIRST 1 ROWS ONLY"},
            "result_summary": "1 row returned",
            "duration_ms": 123,
        }
        canned = _make_agent_result(
            data="Found 1 transaction.",
            tool_calls_log=[tool_log_entry],
            input_tokens=800,
            output_tokens=50,
            confidence=5.0,
        )

        original_init = agent_runner.UnifiedAgent.__init__

        def _init_with_canned(self, *a, **kw):
            original_init(self, *a, **kw)
            self._canned_result = canned

        with patch.object(agent_runner.UnifiedAgent, "__init__", _init_with_canned):
            result = await agent_runner.run_agent(
                tenant_id=_TENANT_ID,
                question="Get the latest transaction",
                db=db_mock,
            )

        assert result.success is True
        assert result.num_steps == 1
        assert len(result.tool_calls) == 1
        entry = result.tool_calls[0]
        assert entry["name"] == "netsuite_suiteql"
        assert "query" in entry["input"]
        # Should include a short result preview string
        assert isinstance(entry["result_preview"], str)
        assert entry["result_preview"]


# ---------------------------------------------------------------------------
# Error path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRunAgentExceptionPath:
    async def test_adapter_raises_returns_failure_result(
        self,
        patched_deps,
        db_mock,
    ):
        from app.services.benchmarks import agent_runner

        original_init = agent_runner.UnifiedAgent.__init__

        def _init_with_raise(self, *a, **kw):
            original_init(self, *a, **kw)
            self._raise_on_run = RuntimeError("simulated anthropic failure")

        with patch.object(agent_runner.UnifiedAgent, "__init__", _init_with_raise):
            result = await agent_runner.run_agent(
                tenant_id=_TENANT_ID,
                question="Anything",
                db=db_mock,
            )

        assert result.success is False
        assert result.error is not None
        assert "simulated anthropic failure" in result.error
        # Even on error we should still get numeric defaults
        assert result.input_tokens == 0
        assert result.output_tokens == 0
        assert result.cost_usd == 0.0
        # latency_ms should still be captured
        assert result.latency_ms >= 0


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestContextAssembly:
    async def test_context_chars_nonzero_after_setup(self, patched_deps, db_mock):
        """system_prompt should have some content after the runner wires things up."""
        from app.services.benchmarks import agent_runner

        result = await agent_runner.run_agent(
            tenant_id=_TENANT_ID,
            question="Trivial question",
            db=db_mock,
        )
        assert result.context_chars > 0

    async def test_handles_missing_connectors_gracefully(
        self,
        patched_deps,
        db_mock,
    ):
        """run_agent must not fail when there are zero connectors."""
        from app.services.benchmarks import agent_runner

        # patched_deps already returns [] for connectors
        result = await agent_runner.run_agent(
            tenant_id=_TENANT_ID,
            question="Trivial question",
            db=db_mock,
        )
        # Doesn't need success=True — just must not raise.
        assert isinstance(result.latency_ms, int)
