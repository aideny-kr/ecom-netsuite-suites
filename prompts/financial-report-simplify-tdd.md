# TDD Sprint: Simplify Financial Reports via NetSuite MCP Native Reports

## Context & Problem

The financial report pipeline has become a Rube Goldberg machine with three competing control paths:
1. **Pre-execution** — regex-parses user message, runs SQL template, dumps 200 rows of JSON into the task
2. **tool_choice forcing** — forces LLM to call `netsuite_financial_report` on step 0
3. **Prompt instruction** — appends "[FINANCIAL REPORT MODE] you MUST use..." text

This causes:
- **Context pollution**: 40-50K tokens per financial query (system prompt + domain knowledge + schema injection + pre-execution data dump + financial mode instructions)
- **Tenant rule ignorance**: Model ignores tenant-specific rules because they're buried under layers of generic instructions
- **High cost**: ~$100/10 days in testing alone
- **Worsening results**: Each "fix" added more tokens, making the model less reliable

## Solution: Replace with NetSuite MCP Native Reports

NetSuite's MCP Standard Tools SuiteApp provides:
- `ns_runReport` — Execute native NetSuite reports (Income Statement, Balance Sheet, Trial Balance, GL)
- `ns_runSavedSearch` — Execute saved searches by ID
- `ns_listAllReports` — Discover available reports

These tools run NetSuite's own accounting engine — correct sign conventions, consolidation, elimination accounts, multi-book handling — all server-side. Zero SuiteQL generation needed.

## Architecture After This Sprint

```
User: "Show me the income statement for Feb 2026"
    ↓
Orchestrator detects FINANCIAL_REPORT intent (existing heuristic — keep this)
    ↓
Strip context to minimal: NO full SuiteQL system prompt, NO schema injection,
NO domain knowledge chunks for financial queries. Just the financial agent prompt.
    ↓
Agent sees 2 tools only: ns_runReport, ns_runSavedSearch
    (netsuite_suiteql REMOVED from tool list for financial queries)
    ↓
Agent calls ns_runReport with report ID + date range
    ↓
NetSuite returns structured financial data
    ↓
Agent formats and presents results (what LLMs are good at)
```

**Token budget target: <3,000 tokens for system prompt on financial queries** (down from 40-50K).

## Key Files to Modify

| File | Action |
|------|--------|
| `backend/app/services/chat/orchestrator.py` | Remove pre-execution path, remove financial mode task augmentation, add tool filtering for financial intent |
| `backend/app/mcp/tools/netsuite_financial_report.py` | **Replace entirely** — new thin wrapper that calls `ns_runReport` or `ns_runSavedSearch` via MCP client |
| `backend/app/mcp/registry.py` | Update `netsuite.financial_report` registration to point to new implementation |
| `backend/app/services/chat/agents/unified_agent.py` | Add `get_financial_tools()` method that returns only report-related tools |
| `backend/app/services/chat/agents/base_agent.py` | No changes needed — tool_choice threading already works |
| `backend/app/services/mcp_client_service.py` | No changes — `call_external_mcp_tool()` already supports any MCP tool name |

## Key Files to Read First (DO NOT SKIP)

Before writing ANY code, read these files completely to understand current patterns:

1. **`skills/netsuite-mcp/SKILL.md`** — **READ THIS FIRST.** Complete reference for all 7 NetSuite MCP tools, their parameters, decision tree, and the orchestrator injection pattern that needs fixing. This is the authoritative guide.
2. `backend/app/services/mcp_client_service.py` — The MCP client that calls external tools. `call_external_mcp_tool(connector, tool_name, tool_params, db)` is the function you'll use. It handles OAuth token refresh, timeout (15s), and JSON parsing.
3. `backend/app/services/chat/orchestrator.py` — Lines 349-373 are the BROKEN ext__ detection (only detects "suiteql" — must detect ALL 7 MCP tools). Lines 489-720 are the unified agent flow. Lines 639-698 are the pre-execution path to REMOVE. Line 711 is the tool_choice forcing.
4. `backend/app/services/chat/tools.py` — How external MCP tools are surfaced to the agent. `build_external_tool_definitions()` already works — the agent ALREADY receives all 7 MCP tools in its tool list. The problem is the agent has no guidance on when to use them.
5. `backend/app/mcp/tools/netsuite_financial_report.py` — Current SQL template approach to REPLACE.
6. `backend/app/mcp/registry.py` — Tool registration pattern.
7. `backend/app/services/chat/agents/unified_agent.py` — `_UNIFIED_TOOL_NAMES` on line 38 and the system prompt.
8. `backend/app/services/chat/nodes.py` — `ALLOWED_CHAT_TOOLS` frozenset.
9. `CLAUDE.md` — Project patterns, especially the API endpoint and service patterns.

## Critical Context: MCP Tools Already Available

The NetSuite MCP connector is already connected and tool discovery has already run. The agent
ALREADY receives all 7 MCP tools (`ns_runReport`, `ns_runSavedSearch`, `ns_listAllReports`,
`ns_listSavedSearches`, `ns_runCustomSuiteQL`, `ns_getSuiteQLMetadata`, `ns_getSubsidiaries`)
as `ext__` prefixed tools in its tool list via `build_external_tool_definitions()`.

The agent just doesn't know what they're for or when to use them because the orchestrator
(lines 354-355) only detects tools with "suiteql" in the name. Fix this FIRST.

## Constraints

- **The MCP connector for each tenant is stored in `mcp_connectors` table.** To call `ns_runReport`, you need the connector object for the tenant. Look at how `external_mcp_suiteql` already resolves the connector in `backend/app/mcp/tools/netsuite_suiteql.py`.
- **`call_external_mcp_tool()` has a 15-second timeout.** Large reports may need a higher timeout. Consider making timeout configurable per tool.
- **`ns_runReport` parameters** (based on NetSuite MCP docs): `reportId` (string, required), `startDate` (optional), `endDate` (optional), `subsidiaryId` (optional). Date format is likely `MM/DD/YYYY` or `YYYY-MM-DD` — test both.
- **Report IDs are account-specific.** Each NetSuite account has different report IDs. Use `ns_listAllReports` to discover them, cache in tenant config or a new table.
- **Balance Sheet has no start date** — it's always inception-to-date. Only pass `endDate`.
- **Fallback**: If `ns_runReport` is not available (older NetSuite accounts without MCP Standard Tools), fall back to `ns_runSavedSearch`. If neither available, fall back to the existing SQL template approach (keep it as a last resort, don't delete the templates yet).

---

## Cycle 1: Report Discovery & Caching

### Goal
Create a service that discovers available NetSuite reports via `ns_listAllReports` and caches the mapping of report type → report ID per tenant.

### Tests First

```python
# backend/tests/test_netsuite_report_discovery.py

import pytest
from unittest.mock import AsyncMock, patch

from app.services.netsuite_report_service import (
    discover_reports,
    get_report_id,
    STANDARD_REPORT_NAMES,
)


class TestStandardReportNames:
    """Verify we know what to look for."""

    def test_standard_report_names_includes_income_statement(self):
        assert "Income Statement" in STANDARD_REPORT_NAMES

    def test_standard_report_names_includes_balance_sheet(self):
        assert "Balance Sheet" in STANDARD_REPORT_NAMES

    def test_standard_report_names_includes_trial_balance(self):
        assert "Trial Balance" in STANDARD_REPORT_NAMES


class TestDiscoverReports:
    """Test report discovery via MCP."""

    @pytest.mark.asyncio
    async def test_discover_reports_calls_mcp_list_all_reports(self):
        """Should call ns_listAllReports via MCP client."""
        mock_connector = AsyncMock()
        mock_db = AsyncMock()

        with patch("app.services.netsuite_report_service.call_external_mcp_tool") as mock_call:
            mock_call.return_value = {
                "reports": [
                    {"id": "101", "name": "Income Statement", "type": "FINANCIAL"},
                    {"id": "102", "name": "Balance Sheet", "type": "FINANCIAL"},
                    {"id": "103", "name": "Trial Balance", "type": "FINANCIAL"},
                ]
            }
            result = await discover_reports(connector=mock_connector, db=mock_db)

            mock_call.assert_called_once_with(
                mock_connector, "ns_listAllReports", {}, mock_db
            )
            assert len(result) >= 3

    @pytest.mark.asyncio
    async def test_discover_reports_handles_mcp_error(self):
        """Should return empty dict on MCP error."""
        mock_connector = AsyncMock()

        with patch("app.services.netsuite_report_service.call_external_mcp_tool") as mock_call:
            mock_call.return_value = {"error": "Tool not found"}
            result = await discover_reports(connector=mock_connector, db=AsyncMock())
            assert result == {}

    @pytest.mark.asyncio
    async def test_discover_reports_filters_financial_reports(self):
        """Should only return reports matching our standard names."""
        mock_connector = AsyncMock()

        with patch("app.services.netsuite_report_service.call_external_mcp_tool") as mock_call:
            mock_call.return_value = {
                "reports": [
                    {"id": "101", "name": "Income Statement", "type": "FINANCIAL"},
                    {"id": "999", "name": "Custom Sales Report", "type": "CUSTOM"},
                ]
            }
            result = await discover_reports(connector=mock_connector, db=AsyncMock())
            assert "income_statement" in result
            assert "custom_sales_report" not in result


class TestGetReportId:
    """Test cached report ID lookup."""

    @pytest.mark.asyncio
    async def test_get_report_id_returns_cached_id(self):
        """Should return cached report ID without calling MCP."""
        # Pre-populate cache
        from app.services.netsuite_report_service import _REPORT_CACHE
        import uuid

        tenant_id = str(uuid.uuid4())
        _REPORT_CACHE[tenant_id] = {
            "income_statement": "101",
            "balance_sheet": "102",
        }

        result = await get_report_id(
            tenant_id=tenant_id,
            report_type="income_statement",
            connector=AsyncMock(),
            db=AsyncMock(),
        )
        assert result == "101"

        # Clean up
        del _REPORT_CACHE[tenant_id]

    @pytest.mark.asyncio
    async def test_get_report_id_discovers_on_cache_miss(self):
        """Should discover reports when cache is empty."""
        with patch("app.services.netsuite_report_service.discover_reports") as mock_discover:
            mock_discover.return_value = {"income_statement": "101"}

            result = await get_report_id(
                tenant_id="new-tenant",
                report_type="income_statement",
                connector=AsyncMock(),
                db=AsyncMock(),
            )
            assert result == "101"
            mock_discover.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_report_id_returns_none_for_unknown_type(self):
        """Should return None for unsupported report types."""
        with patch("app.services.netsuite_report_service.discover_reports") as mock_discover:
            mock_discover.return_value = {"income_statement": "101"}

            result = await get_report_id(
                tenant_id="some-tenant",
                report_type="cash_flow",
                connector=AsyncMock(),
                db=AsyncMock(),
            )
            assert result is None
```

### Implementation

Create `backend/app/services/netsuite_report_service.py`:

```python
"""NetSuite report discovery and execution via MCP native reports.

Replaces the SQL template approach with direct calls to NetSuite's
ns_runReport MCP tool — letting NetSuite handle all accounting logic
(sign conventions, consolidation, elimination, multi-book).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from app.services.mcp_client_service import call_external_mcp_tool

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.models.mcp_connector import McpConnector

# Standard NetSuite financial report names to look for during discovery
STANDARD_REPORT_NAMES: dict[str, str] = {
    "Income Statement": "income_statement",
    "Balance Sheet": "balance_sheet",
    "Trial Balance": "trial_balance",
    # Add more as discovered
}

# In-memory cache: tenant_id → {report_type: report_id}
# TTL: 1 hour (reports don't change often)
_REPORT_CACHE: dict[str, dict[str, str]] = {}
_CACHE_TIMESTAMPS: dict[str, float] = {}
_CACHE_TTL = 3600  # 1 hour


async def discover_reports(
    connector: McpConnector,
    db: AsyncSession,
) -> dict[str, str]:
    """Discover available financial reports via ns_listAllReports.

    Returns: {report_type: report_id} mapping, e.g. {"income_statement": "101"}
    """
    result = await call_external_mcp_tool(connector, "ns_listAllReports", {}, db)

    if "error" in result:
        print(f"[REPORT_DISCOVERY] MCP error: {result['error']}", flush=True)
        return {}

    reports = result.get("reports", [])
    mapping = {}

    for report in reports:
        name = report.get("name", "")
        report_id = str(report.get("id", ""))
        if name in STANDARD_REPORT_NAMES and report_id:
            mapping[STANDARD_REPORT_NAMES[name]] = report_id

    print(f"[REPORT_DISCOVERY] Found {len(mapping)} standard reports: {list(mapping.keys())}", flush=True)
    return mapping


async def get_report_id(
    tenant_id: str,
    report_type: str,
    connector: McpConnector,
    db: AsyncSession,
) -> str | None:
    """Get the NetSuite report ID for a given report type, with caching."""
    # Check cache
    if tenant_id in _REPORT_CACHE:
        cache_age = time.time() - _CACHE_TIMESTAMPS.get(tenant_id, 0)
        if cache_age < _CACHE_TTL:
            return _REPORT_CACHE[tenant_id].get(report_type)

    # Cache miss — discover
    mapping = await discover_reports(connector, db)
    _REPORT_CACHE[tenant_id] = mapping
    _CACHE_TIMESTAMPS[tenant_id] = time.time()

    return mapping.get(report_type)
```

### Verify
```bash
cd backend && .venv/bin/python -m pytest tests/test_netsuite_report_discovery.py -v
```

---

## Cycle 2: MCP Report Execution Wrapper

### Goal
Create the `execute_report()` function that calls `ns_runReport` via MCP and returns structured data. Also add `ns_runSavedSearch` fallback.

### Tests First

```python
# backend/tests/test_netsuite_report_execution.py

import pytest
from unittest.mock import AsyncMock, patch

from app.services.netsuite_report_service import execute_report


class TestExecuteReport:
    """Test report execution via ns_runReport MCP tool."""

    @pytest.mark.asyncio
    async def test_execute_income_statement(self):
        """Should call ns_runReport with correct params for income statement."""
        mock_connector = AsyncMock()

        with patch("app.services.netsuite_report_service.call_external_mcp_tool") as mock_call:
            mock_call.return_value = {
                "columns": ["Account", "Amount"],
                "rows": [
                    {"Account": "4000 Revenue", "Amount": "150000.00"},
                    {"Account": "5000 COGS", "Amount": "80000.00"},
                ],
                "totalRows": 2,
            }
            result = await execute_report(
                connector=mock_connector,
                report_id="101",
                report_type="income_statement",
                period="Feb 2026",
                db=AsyncMock(),
            )

            assert result["success"] is True
            assert len(result["rows"]) == 2
            # Verify ns_runReport was called (not ns_runCustomSuiteQL)
            call_args = mock_call.call_args
            assert call_args[0][1] == "ns_runReport"

    @pytest.mark.asyncio
    async def test_execute_balance_sheet_no_start_date(self):
        """Balance sheet should only pass endDate (inception-to-date)."""
        mock_connector = AsyncMock()

        with patch("app.services.netsuite_report_service.call_external_mcp_tool") as mock_call:
            mock_call.return_value = {"columns": [], "rows": [], "totalRows": 0}

            await execute_report(
                connector=mock_connector,
                report_id="102",
                report_type="balance_sheet",
                period="Feb 2026",
                db=AsyncMock(),
            )

            call_params = mock_call.call_args[0][2]
            assert "startDate" not in call_params
            assert "endDate" in call_params

    @pytest.mark.asyncio
    async def test_execute_income_statement_has_date_range(self):
        """Income statement should pass both startDate and endDate."""
        mock_connector = AsyncMock()

        with patch("app.services.netsuite_report_service.call_external_mcp_tool") as mock_call:
            mock_call.return_value = {"columns": [], "rows": [], "totalRows": 0}

            await execute_report(
                connector=mock_connector,
                report_id="101",
                report_type="income_statement",
                period="Feb 2026",
                db=AsyncMock(),
            )

            call_params = mock_call.call_args[0][2]
            assert "startDate" in call_params
            assert "endDate" in call_params

    @pytest.mark.asyncio
    async def test_execute_report_handles_mcp_error(self):
        """Should return error dict on MCP failure."""
        mock_connector = AsyncMock()

        with patch("app.services.netsuite_report_service.call_external_mcp_tool") as mock_call:
            mock_call.return_value = {"error": "Report not found"}

            result = await execute_report(
                connector=mock_connector,
                report_id="999",
                report_type="income_statement",
                period="Feb 2026",
                db=AsyncMock(),
            )
            assert result["success"] is False
            assert "error" in result

    @pytest.mark.asyncio
    async def test_execute_report_with_subsidiary(self):
        """Should pass subsidiaryId when provided."""
        mock_connector = AsyncMock()

        with patch("app.services.netsuite_report_service.call_external_mcp_tool") as mock_call:
            mock_call.return_value = {"columns": [], "rows": [], "totalRows": 0}

            await execute_report(
                connector=mock_connector,
                report_id="101",
                report_type="income_statement",
                period="Feb 2026",
                subsidiary_id=5,
                db=AsyncMock(),
            )

            call_params = mock_call.call_args[0][2]
            assert call_params.get("subsidiaryId") == 5


class TestPeriodToDateRange:
    """Test period string → date range conversion."""

    def test_single_month(self):
        from app.services.netsuite_report_service import _period_to_date_range
        start, end = _period_to_date_range("Feb 2026")
        assert start == "02/01/2026"
        assert end == "02/28/2026"

    def test_leap_year_february(self):
        from app.services.netsuite_report_service import _period_to_date_range
        start, end = _period_to_date_range("Feb 2024")
        assert end == "02/29/2024"

    def test_multi_month_returns_full_range(self):
        from app.services.netsuite_report_service import _period_to_date_range
        start, end = _period_to_date_range("Jan 2026, Feb 2026, Mar 2026")
        assert start == "01/01/2026"
        assert end == "03/31/2026"

    def test_invalid_period_raises(self):
        from app.services.netsuite_report_service import _period_to_date_range
        with pytest.raises(ValueError):
            _period_to_date_range("invalid")
```

### Implementation

Add to `backend/app/services/netsuite_report_service.py`:

```python
import calendar
import re

_PERIOD_RE = re.compile(r"^([A-Z][a-z]{2})\s(\d{4})$")
_MONTH_MAP = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

# Report types that use inception-to-date (no start date)
_INCEPTION_TO_DATE_REPORTS = {"balance_sheet", "balance_sheet_trend"}


def _period_to_date_range(period: str) -> tuple[str, str]:
    """Convert 'Feb 2026' or 'Jan 2026, Feb 2026, Mar 2026' to (start_date, end_date).

    Returns dates in MM/DD/YYYY format for NetSuite.
    """
    periods = [p.strip() for p in period.split(",")]

    parsed = []
    for p in periods:
        match = _PERIOD_RE.match(p)
        if not match:
            raise ValueError(f"Invalid period: '{p}'. Expected 'Mon YYYY' (e.g., 'Feb 2026').")
        month_num = _MONTH_MAP[match.group(1)]
        year = int(match.group(2))
        parsed.append((year, month_num))

    parsed.sort()
    first_year, first_month = parsed[0]
    last_year, last_month = parsed[-1]

    start_date = f"{first_month:02d}/01/{first_year}"
    last_day = calendar.monthrange(last_year, last_month)[1]
    end_date = f"{last_month:02d}/{last_day:02d}/{last_year}"

    return start_date, end_date


async def execute_report(
    connector: McpConnector,
    report_id: str,
    report_type: str,
    period: str,
    db: AsyncSession,
    subsidiary_id: int | None = None,
) -> dict:
    """Execute a NetSuite native report via ns_runReport MCP tool.

    This lets NetSuite handle all accounting logic — sign conventions,
    consolidation, elimination, multi-book — server-side.
    """
    try:
        start_date, end_date = _period_to_date_range(period)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    params: dict = {"reportId": report_id}

    # Balance sheet = inception-to-date, only endDate
    if report_type not in _INCEPTION_TO_DATE_REPORTS:
        params["startDate"] = start_date
    params["endDate"] = end_date

    if subsidiary_id is not None:
        params["subsidiaryId"] = subsidiary_id

    print(f"[REPORT_EXEC] ns_runReport: type={report_type} params={params}", flush=True)

    result = await call_external_mcp_tool(connector, "ns_runReport", params, db)

    if "error" in result:
        return {"success": False, "error": result["error"], "report_type": report_type}

    return {
        "success": True,
        "report_type": report_type,
        "period": period,
        "columns": result.get("columns", []),
        "rows": result.get("rows", result.get("items", [])),
        "total_rows": result.get("totalRows", result.get("total_rows", 0)),
    }
```

### Verify
```bash
cd backend && .venv/bin/python -m pytest tests/test_netsuite_report_execution.py -v
```

---

## Cycle 3: Orchestrator Simplification — Remove Pre-Execution & Context Bloat

### Goal
Strip the orchestrator of the pre-execution path, financial mode task augmentation, and excessive context injection for financial queries. The agent should receive a minimal prompt and 2 tools.

### Tests First

```python
# backend/tests/test_orchestrator_financial_simplified.py

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from app.services.chat.orchestrator import _build_financial_mode_task


class TestFinancialModeSimplified:
    """Verify the simplified financial mode."""

    def test_build_financial_mode_task_is_concise(self):
        """Financial mode task should be under 500 chars (was 1000+)."""
        task = _build_financial_mode_task("Show me the income statement for Feb 2026")
        assert len(task) < 500

    def test_build_financial_mode_task_mentions_ns_run_report(self):
        """Should reference ns_runReport, not SQL templates."""
        task = _build_financial_mode_task("Show me the P&L for last month")
        assert "ns_runReport" in task or "netsuite_report" in task
        assert "SuiteQL" not in task
        assert "TAL" not in task
        assert "BUILTIN.CONSOLIDATE" not in task


class TestOrchestratorNoPreExecution:
    """Verify pre-execution path is removed."""

    def test_no_pre_execution_imports(self):
        """orchestrator.py should not import parse_report_intent."""
        import inspect
        from app.services.chat import orchestrator
        source = inspect.getsource(orchestrator)
        # The inline import of parse_report_intent should be gone
        assert "parse_report_intent" not in source

    def test_no_json_dump_in_task(self):
        """Financial task should never contain raw JSON data dumps."""
        task = _build_financial_mode_task("Income statement Q1 2026")
        assert "items_json" not in task
        assert '"acctnumber"' not in task
        assert "DATA ALREADY FETCHED" not in task
```

### Implementation Changes

In `backend/app/services/chat/orchestrator.py`:

**1. Replace `_build_financial_mode_task()` (lines 32-58):**

```python
def _build_financial_mode_task(user_message: str) -> str:
    """Build a minimal task for financial report queries.

    Keep it short — the agent only needs to know to call the report tool.
    No SQL templates, no TAL instructions, no sign convention rules.
    NetSuite handles all accounting logic server-side.
    """
    return (
        f"{user_message}\n\n"
        "[FINANCIAL REPORT]\n"
        "Call the netsuite_report tool to fetch this data. "
        "Parse the user's request to determine report_type and period. "
        "Present the results in a clear, formatted table with sections and totals."
    )
```

**2. Remove the entire pre-execution block (lines 639-698).** Replace with:

```python
                # Financial report mode: minimal context, report tool only
                unified_task = sanitized_input
                if not _is_chitchat and is_financial:
                    unified_task = _build_financial_mode_task(sanitized_input)
                    print("[UNIFIED] Financial report mode activated (simplified)", flush=True)
```

**3. Remove tool_choice forcing (line 711).** The tool list filtering (Cycle 4) handles this better:

```python
                async for event_type, payload in unified_agent.run_streaming(
                    task=unified_task,
                    context=context,
                    db=db,
                    adapter=specialist_adapter,
                    model=settings.MULTI_AGENT_SQL_MODEL,
                    conversation_history=history_messages,
                    # tool_choice removed — tool filtering does this job
                ):
```

**4. Skip domain knowledge and schema injection for financial queries:**

Where domain knowledge is retrieved (~line 539), add:
```python
                    if is_financial:
                        # Financial queries use native NetSuite reports — no domain knowledge needed
                        context["domain_knowledge"] = []
                    else:
                        dk_results = await retrieve_domain_knowledge(...)
                        context["domain_knowledge"] = [r["raw_text"] for r in dk_results]
```

### Verify
```bash
cd backend && .venv/bin/python -m pytest tests/test_orchestrator_financial_simplified.py -v
```

---

## Cycle 4: Agent Tool Filtering for Financial Mode

### Goal
When financial intent is detected, the agent should only see report-related tools — NOT `netsuite_suiteql`. This eliminates the "familiarity bias" problem entirely.

### Tests First

```python
# backend/tests/test_agent_financial_tools.py

import pytest
from app.services.chat.agents.unified_agent import UnifiedAgent


class TestFinancialToolFiltering:
    """Verify tool filtering for financial queries."""

    def test_get_financial_tools_excludes_suiteql(self):
        """Financial mode should NOT include netsuite_suiteql."""
        agent = UnifiedAgent()
        tools = agent.get_financial_tools()
        tool_names = {t["name"] if isinstance(t, dict) else t.name for t in tools}
        assert "netsuite_suiteql" not in tool_names

    def test_get_financial_tools_includes_report_tool(self):
        """Financial mode should include netsuite_report."""
        agent = UnifiedAgent()
        tools = agent.get_financial_tools()
        tool_names = {t["name"] if isinstance(t, dict) else t.name for t in tools}
        assert "netsuite_report" in tool_names

    def test_get_financial_tools_is_small_set(self):
        """Financial tools should be a small set — 3 or fewer."""
        agent = UnifiedAgent()
        tools = agent.get_financial_tools()
        assert len(tools) <= 3

    def test_default_tools_still_include_suiteql(self):
        """Non-financial queries should still have full tool set."""
        agent = UnifiedAgent()
        default_tools = agent.get_tools()  # or however tools are normally resolved
        tool_names = {t["name"] if isinstance(t, dict) else t.name for t in default_tools}
        assert "netsuite_suiteql" in tool_names
```

### Implementation

In `backend/app/services/chat/agents/unified_agent.py`:

```python
_FINANCIAL_TOOL_NAMES = frozenset({
    "netsuite_report",       # ns_runReport wrapper
    "rag_search",            # For doc lookups if user asks follow-up questions
})
```

Add a `get_financial_tools()` method that returns only the financial tool definitions.

In the orchestrator, when `is_financial`:
```python
# Override tools for financial mode
if is_financial:
    unified_agent.override_tools(unified_agent.get_financial_tools())
```

### Verify
```bash
cd backend && .venv/bin/python -m pytest tests/test_agent_financial_tools.py -v
```

---

## Cycle 5: Register New Report Tool in MCP Registry

### Goal
Register `netsuite_report` (backed by `netsuite_report_service.execute_report()`) in the MCP registry and wire up the MCP connector resolution.

### Tests First

```python
# backend/tests/test_netsuite_report_tool_registration.py

import pytest
from app.mcp.registry import TOOL_REGISTRY


class TestReportToolRegistration:
    """Verify report tool is registered."""

    def test_netsuite_report_in_registry(self):
        """netsuite.report should be in the tool registry."""
        assert "netsuite.report" in TOOL_REGISTRY

    def test_netsuite_report_has_execute(self):
        """Should have an execute function."""
        assert callable(TOOL_REGISTRY["netsuite.report"]["execute"])

    def test_netsuite_report_params_schema(self):
        """Should require report_type and period."""
        schema = TOOL_REGISTRY["netsuite.report"]["params_schema"]
        assert "report_type" in schema
        assert "period" in schema
        assert schema["report_type"]["required"] is True
        assert schema["period"]["required"] is True

    def test_old_financial_report_removed_or_deprecated(self):
        """The old SQL template tool should be removed or marked deprecated."""
        if "netsuite.financial_report" in TOOL_REGISTRY:
            desc = TOOL_REGISTRY["netsuite.financial_report"]["description"]
            assert "deprecated" in desc.lower() or "legacy" in desc.lower()
```

### Implementation

In `backend/app/mcp/registry.py`, add the new tool:

```python
    "netsuite.report": {
        "description": (
            "Run a NetSuite native financial report (Income Statement, Balance Sheet, "
            "Trial Balance). Uses NetSuite's own accounting engine — correct sign "
            "conventions, consolidation, and period handling guaranteed."
        ),
        "execute": netsuite_report.execute,  # New wrapper
        "params_schema": {
            "report_type": {
                "type": "string",
                "required": True,
                "description": "Report type: 'income_statement', 'balance_sheet', 'trial_balance'",
                "enum": ["income_statement", "balance_sheet", "trial_balance"],
            },
            "period": {
                "type": "string",
                "required": True,
                "description": "Period like 'Feb 2026' or 'Jan 2026, Feb 2026, Mar 2026' for multi-period",
            },
            "subsidiary_id": {
                "type": "integer",
                "required": False,
                "description": "Filter to a specific subsidiary",
            },
        },
    },
```

Create `backend/app/mcp/tools/netsuite_report.py` as a thin wrapper that:
1. Resolves the MCP connector for the tenant (same pattern as `netsuite_suiteql.py`)
2. Calls `get_report_id()` to look up the NetSuite report ID
3. Calls `execute_report()` with the resolved ID
4. Returns structured results

### Verify
```bash
cd backend && .venv/bin/python -m pytest tests/test_netsuite_report_tool_registration.py -v
```

---

## Cycle 6: Fallback Chain & Integration Test

### Goal
Ensure graceful fallback: `ns_runReport` → `ns_runSavedSearch` → SQL templates (legacy). Test the full flow end-to-end.

### Tests First

```python
# backend/tests/test_report_fallback_chain.py

import pytest
from unittest.mock import AsyncMock, patch


class TestFallbackChain:
    """Test the three-level fallback: ns_runReport → ns_runSavedSearch → SQL template."""

    @pytest.mark.asyncio
    async def test_primary_ns_run_report(self):
        """Should use ns_runReport as primary path."""
        from app.mcp.tools.netsuite_report import execute

        with patch("app.services.netsuite_report_service.get_report_id") as mock_id, \
             patch("app.services.netsuite_report_service.execute_report") as mock_exec:

            mock_id.return_value = "101"
            mock_exec.return_value = {"success": True, "rows": [{"Account": "Revenue", "Amount": "1000"}]}

            result = await execute(
                params={"report_type": "income_statement", "period": "Feb 2026"},
                context={"tenant_id": "test-tenant", "db": AsyncMock()},
            )
            assert result["success"] is True
            mock_exec.assert_called_once()

    @pytest.mark.asyncio
    async def test_fallback_to_sql_template_when_no_mcp(self):
        """When ns_runReport unavailable, fall back to SQL templates."""
        from app.mcp.tools.netsuite_report import execute

        with patch("app.services.netsuite_report_service.get_report_id") as mock_id, \
             patch("app.mcp.tools.netsuite_financial_report.execute") as mock_legacy:

            mock_id.return_value = None  # No report ID found
            mock_legacy.return_value = {"success": True, "rows": []}

            result = await execute(
                params={"report_type": "income_statement", "period": "Feb 2026"},
                context={"tenant_id": "test-tenant", "db": AsyncMock()},
            )
            # Should have fallen back to legacy SQL template
            mock_legacy.assert_called_once()

    @pytest.mark.asyncio
    async def test_fallback_on_mcp_error(self):
        """When ns_runReport errors, fall back to SQL templates."""
        from app.mcp.tools.netsuite_report import execute

        with patch("app.services.netsuite_report_service.get_report_id") as mock_id, \
             patch("app.services.netsuite_report_service.execute_report") as mock_exec, \
             patch("app.mcp.tools.netsuite_financial_report.execute") as mock_legacy:

            mock_id.return_value = "101"
            mock_exec.return_value = {"success": False, "error": "MCP timeout"}
            mock_legacy.return_value = {"success": True, "rows": []}

            result = await execute(
                params={"report_type": "income_statement", "period": "Feb 2026"},
                context={"tenant_id": "test-tenant", "db": AsyncMock()},
            )
            mock_legacy.assert_called_once()


class TestTokenReduction:
    """Verify the context size reduction goal."""

    def test_financial_mode_task_under_500_chars(self):
        """Financial mode task augmentation should be minimal."""
        from app.services.chat.orchestrator import _build_financial_mode_task
        task = _build_financial_mode_task("Show me the income statement for Feb 2026")
        assert len(task) < 500

    def test_no_domain_knowledge_in_financial_context(self):
        """Financial queries should skip domain knowledge injection."""
        # This verifies the orchestrator doesn't add domain_knowledge for financial queries
        # Implementation test — check that context["domain_knowledge"] is [] when is_financial
        pass  # Tested via orchestrator integration test
```

### Verify
```bash
cd backend && .venv/bin/python -m pytest tests/test_report_fallback_chain.py -v
```

---

## Final Verification

After all cycles pass:

```bash
cd backend && .venv/bin/python -m pytest tests/ -v --tb=short -q 2>&1 | tail -20
```

All existing tests should still pass. The key behavioral changes:

1. **Financial queries** → `ns_runReport` via MCP (zero SuiteQL generation)
2. **Non-financial queries** → unchanged (full SuiteQL system prompt + tools)
3. **Fallback** → SQL templates still available if MCP reports unavailable
4. **Token usage** → <3,000 tokens for financial queries (down from 40-50K)
5. **Pre-execution path** → REMOVED (no more JSON dumps in task)
6. **tool_choice forcing** → REMOVED (tool filtering handles this)
7. **Tenant rules** → More visible because not buried under context bloat

## Files Changed Summary

| File | Change |
|------|--------|
| `backend/app/services/netsuite_report_service.py` | **NEW** — Report discovery, caching, execution |
| `backend/app/mcp/tools/netsuite_report.py` | **NEW** — Thin MCP tool wrapper with fallback chain |
| `backend/app/services/chat/orchestrator.py` | **SIMPLIFIED** — Remove pre-execution, remove JSON dump, skip domain knowledge for financial |
| `backend/app/services/chat/agents/unified_agent.py` | **MODIFIED** — Add `get_financial_tools()`, `_FINANCIAL_TOOL_NAMES` |
| `backend/app/mcp/registry.py` | **MODIFIED** — Register `netsuite.report`, deprecate `netsuite.financial_report` |
| `backend/app/services/chat/nodes.py` | **MODIFIED** — Add `netsuite.report` to `ALLOWED_CHAT_TOOLS` |
| `backend/app/mcp/tools/netsuite_financial_report.py` | **KEEP** — Legacy fallback (do NOT delete) |

## DO NOT

- Do NOT delete `netsuite_financial_report.py` — it's the fallback for accounts without MCP Standard Tools
- Do NOT modify the SuiteQL agent system prompt — non-financial queries still need it
- Do NOT change `base_agent.py` — the tool_choice threading is fine, just not needed for this flow
- Do NOT add new domain knowledge chunks — the whole point is to REDUCE context
- Do NOT add `parse_report_intent()` back — let the LLM parse natural language (it's good at this)
