# Knowledge-Driven Unified Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the three-tier agent routing system with knowledge profiles that inject domain context based on which tools are connected, enabling model self-routing via tool_use.

**Architecture:** One unified agent with all connected tools. Knowledge profiles (YAML configs) inject domain-specific prompt fragments and trigger RAG retrieval for matching partitions. Prompt assembler composes: base prompt + tool inventory + active profile fragments + tenant context. Safety stays at the tool layer (HITL, read-only SQL, policy). Source pin kept as lightweight prompt hint.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, Pydantic v2, PyYAML, fnmatch (stdlib), pytest

**Eng Review Decisions Baked In:**
1. Assembler triggers batched RAG retrieval per profile (single DB query with IN clause)
2. Source pins kept as prompt hint, routing logic stripped
3. Chitchat regex → importance_tier CASUAL gating
4. coordinator.py + legacy multi-agent path deleted
5. Orchestrator path tests expanded BEFORE any deletion
6. All 16 test gaps from eng review covered

**Scope:** Phases 1-2 only (add profiles + delete routing). Cross-source queries and Google Sheets deferred.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `backend/app/services/chat/knowledge_profiles/__init__.py` (Create) | Package init |
| `backend/app/services/chat/knowledge_profiles/loader.py` (Create) | Load YAML profiles, `KnowledgeProfile` model, `matches_tools()` with fnmatch |
| `backend/app/services/chat/knowledge_profiles/bigquery.yaml` (Create) | BI analytics profile — extracted from `bi_agent_prompt.md` |
| `backend/app/services/chat/knowledge_profiles/pricing.yaml` (Create) | Pricing profile — extracted from `pricing_agent_prompt.md` |
| `backend/app/services/chat/knowledge_profiles/reconciliation.yaml` (Create) | Recon profile — extracted from `recon_agent.md` |
| `backend/app/services/chat/knowledge_profiles/netsuite_writes.yaml` (Create) | Write ops profile — HITL instructions |
| `backend/app/services/chat/prompt_assembler.py` (Create) | Compose final prompt from base + profiles + RAG + disambiguation |
| `backend/app/services/chat/orchestrator.py` (Modify) | Replace routing with profile-based assembly, delete veto phrases/picker/chitchat |
| `backend/app/services/chat/routing/` (Delete) | Entire directory |
| `backend/app/services/chat/source_picker.py` (Delete) | Source picker scorer |
| `backend/app/services/chat/coordinator.py` (Delete) | Legacy multi-agent coordinator |
| `backend/app/services/chat/agents/agent_registry.py` (Delete) | Agent registry |
| `backend/app/services/chat/agents/specialized_agent.py` (Delete) | Specialized agent class |
| `backend/app/services/chat/agents/tool_filter.py` (Delete) | Tool filter |
| `backend/app/services/chat/agents/configs/bi_agent.yaml` (Delete) | BI agent config |
| `backend/app/services/chat/agents/configs/pricing_agent.yaml` (Delete) | Pricing agent config |
| `backend/app/services/chat/agents/configs/recon_agent.yaml` (Delete) | Recon agent config |
| `backend/app/services/chat/agents/prompts/bi_agent_prompt.md` (Delete) | BI prompt (extracted to profile) |
| `backend/app/services/chat/agents/prompts/pricing_agent_prompt.md` (Delete) | Pricing prompt (extracted to profile) |
| `backend/app/services/chat/agents/prompts/recon_agent.md` (Delete) | Recon prompt (extracted to profile) |
| `frontend/src/components/chat/source-picker-card.tsx` (Delete) | Source picker UI |
| `frontend/src/app/(dashboard)/chat/page.tsx` (Modify) | Remove source picker handling |
| `frontend/src/components/chat/message-list.tsx` (Modify) | Remove source picker rendering |
| `backend/tests/test_knowledge_profile_loader.py` (Create) | Loader + matches_tools tests |
| `backend/tests/test_prompt_assembler.py` (Create) | Assembler tests |
| `backend/tests/test_orchestrator_paths.py` (Modify) | Expand path coverage before deletion |

---

### Task 1: Knowledge Profile Loader + Tests

**Files:**
- Create: `backend/app/services/chat/knowledge_profiles/__init__.py`
- Create: `backend/app/services/chat/knowledge_profiles/loader.py`
- Test: `backend/tests/test_knowledge_profile_loader.py`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_knowledge_profile_loader.py
import pytest
from pathlib import Path
from app.services.chat.knowledge_profiles.loader import KnowledgeProfile, load_all_profiles


class TestKnowledgeProfileModel:
    def test_exact_match(self):
        p = KnowledgeProfile(
            profile_id="bigquery",
            display_name="BigQuery",
            trigger_tools=["bigquery_sql", "bigquery_schema"],
            prompt_fragment="## BQ Context",
            rag_partitions=["bi/schema-docs"],
        )
        assert p.matches_tools({"bigquery_sql", "netsuite_suiteql"}) is True

    def test_no_match(self):
        p = KnowledgeProfile(
            profile_id="bigquery",
            display_name="BigQuery",
            trigger_tools=["bigquery_sql"],
            prompt_fragment="## BQ",
            rag_partitions=[],
        )
        assert p.matches_tools({"netsuite_suiteql", "rag_search"}) is False

    def test_glob_match(self):
        p = KnowledgeProfile(
            profile_id="netsuite_writes",
            display_name="NS Writes",
            trigger_tools=["ext__*__ns_createRecord", "ext__*__ns_updateRecord"],
            prompt_fragment="## Writes",
            rag_partitions=[],
        )
        assert p.matches_tools({"ext__a1b2c3d4e5f67890a1b2c3d4e5f67890__ns_createRecord"}) is True

    def test_glob_no_match(self):
        p = KnowledgeProfile(
            profile_id="netsuite_writes",
            display_name="NS Writes",
            trigger_tools=["ext__*__ns_createRecord"],
            prompt_fragment="## Writes",
            rag_partitions=[],
        )
        assert p.matches_tools({"ext__a1b2c3d4e5f67890a1b2c3d4e5f67890__ns_runCustomSuiteQL"}) is False

    def test_empty_trigger_tools(self):
        p = KnowledgeProfile(
            profile_id="empty",
            display_name="Empty",
            trigger_tools=[],
            prompt_fragment="",
            rag_partitions=[],
        )
        assert p.matches_tools({"bigquery_sql"}) is False


class TestLoadAllProfiles:
    def test_loads_yaml_files(self, tmp_path):
        (tmp_path / "test.yaml").write_text(
            "profile_id: test\n"
            "display_name: Test\n"
            "trigger_tools:\n  - foo_tool\n"
            "prompt_fragment: '## Test'\n"
            "rag_partitions: []\n"
        )
        profiles = load_all_profiles(tmp_path)
        assert len(profiles) == 1
        assert profiles[0].profile_id == "test"

    def test_skips_malformed_yaml(self, tmp_path):
        (tmp_path / "good.yaml").write_text(
            "profile_id: good\ndisplay_name: Good\n"
            "trigger_tools: [foo]\nprompt_fragment: ok\nrag_partitions: []\n"
        )
        (tmp_path / "bad.yaml").write_text("not: valid: yaml: [[[")
        profiles = load_all_profiles(tmp_path)
        assert len(profiles) == 1

    def test_empty_directory(self, tmp_path):
        profiles = load_all_profiles(tmp_path)
        assert profiles == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/python -m pytest tests/test_knowledge_profile_loader.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement the loader**

```python
# backend/app/services/chat/knowledge_profiles/__init__.py
from .loader import KnowledgeProfile, load_all_profiles

__all__ = ["KnowledgeProfile", "load_all_profiles"]
```

```python
# backend/app/services/chat/knowledge_profiles/loader.py
"""Load knowledge profiles from YAML files.

Each profile declares which tools trigger it, a prompt fragment to inject,
and RAG partitions to query for domain knowledge.
"""
from __future__ import annotations

import fnmatch
import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class KnowledgeProfile(BaseModel):
    """A domain knowledge profile triggered by tool presence."""

    profile_id: str
    display_name: str
    trigger_tools: list[str] = Field(default_factory=list)
    prompt_fragment: str = ""
    rag_partitions: list[str] = Field(default_factory=list)

    def matches_tools(self, tool_names: set[str]) -> bool:
        """Return True if any trigger tool is present in tool_names.

        Supports exact match and fnmatch glob patterns (e.g. ext__*__ns_createRecord).
        """
        for trigger in self.trigger_tools:
            if "*" in trigger or "?" in trigger:
                if any(fnmatch.fnmatch(name, trigger) for name in tool_names):
                    return True
            elif trigger in tool_names:
                return True
        return False


def load_all_profiles(directory: str | Path | None = None) -> list[KnowledgeProfile]:
    """Load all YAML knowledge profiles from a directory.

    Skips malformed files with a warning instead of crashing.
    """
    if directory is None:
        directory = Path(__file__).parent
    directory = Path(directory)

    if not directory.is_dir():
        return []

    profiles: list[KnowledgeProfile] = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            with open(path) as f:
                data = yaml.safe_load(f)
            if not isinstance(data, dict):
                logger.warning("knowledge_profile.skip_non_dict: %s", path.name)
                continue
            profiles.append(KnowledgeProfile(**data))
        except Exception:
            logger.warning("knowledge_profile.skip_malformed: %s", path.name, exc_info=True)
    return profiles
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/python -m pytest tests/test_knowledge_profile_loader.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/chat/knowledge_profiles/ backend/tests/test_knowledge_profile_loader.py
git commit -m "feat(agent): add knowledge profile loader with YAML parsing and fnmatch tool matching"
```

---

### Task 2: Create Knowledge Profile YAML Files

**Files:**
- Create: `backend/app/services/chat/knowledge_profiles/bigquery.yaml`
- Create: `backend/app/services/chat/knowledge_profiles/pricing.yaml`
- Create: `backend/app/services/chat/knowledge_profiles/reconciliation.yaml`
- Create: `backend/app/services/chat/knowledge_profiles/netsuite_writes.yaml`

Extract domain knowledge from existing specialist agent prompts (`agents/prompts/*.md`) into YAML profiles. Each profile contains the prompt fragment that was previously in the specialist prompt, plus the RAG partitions from the agent YAML config.

- [ ] **Step 1: Create bigquery.yaml**

Extract from `agents/prompts/bi_agent_prompt.md` (the workflow, SQL rules, chart heuristic) and `agents/configs/bi_agent.yaml` (rag_partitions). Write to `backend/app/services/chat/knowledge_profiles/bigquery.yaml`:

```yaml
profile_id: bigquery
display_name: "BigQuery Analytics"
trigger_tools:
  - bigquery_sql
  - bigquery_schema
  - bigquery_cost_estimate
prompt_fragment: |
  ## BigQuery Analytics Context

  You are also a senior BI analyst. When the user asks analytics questions and BigQuery tools are available, follow this workflow:

  1. **Schema Discovery**: ALWAYS call bigquery_schema first to discover exact column names. Column names vary per tenant.
  2. **Cost Check**: For large queries, call bigquery_cost_estimate first.
  3. **Write SQL**: BigQuery Standard SQL (NOT legacy SQL, NOT SuiteQL). Use ONLY column names from schema result.
  4. **Execute**: Call bigquery_sql.
  5. **Pivot** (optional): Call pivot_query_result with dialect: "bigquery" for cross-tab views.
  6. **Visualize**: If results have 2+ rows with dimension + measure, emit a <chart> block.
  7. **Narrate**: Lead with the headline, call out anomalies, suggest follow-ups.

  BigQuery SQL rules (these prevent production failures):
  - Backtick identifiers: `project.dataset.table`
  - Use LIMIT N (not FETCH FIRST)
  - DATE_TRUNC(date_col, MONTH) for period grouping
  - SAFE_DIVIDE() to prevent division by zero
  - Always add date range filters on large tables
  - APPROX_QUANTILES(col, 100)[OFFSET(50)] for median (not PERCENTILE_CONT with GROUP BY)
  - Use email column for customer-level analysis (no customer_id column)
rag_partitions:
  - bi/schema-docs
  - bi/common-queries
  - bi/metric-definitions
```

- [ ] **Step 2: Create pricing.yaml**

Extract from `agents/prompts/pricing_agent_prompt.md` and `agents/configs/pricing_agent.yaml`:

```yaml
profile_id: pricing
display_name: "Pricing Engine"
trigger_tools:
  - pricing_convert
  - pricing_config_read
  - pricing_config_update
  - pricing_export
prompt_fragment: |
  ## Pricing Engine Context

  You have access to the pricing conversion engine for margin calculations, tariff analysis, price list queries, and currency conversion.

  Capabilities:
  - Margin Analysis: gross/net margins on items, product lines, customers
  - Tariff Impact: tariffs and duties effect on landed cost
  - Price List Queries: item rates, base prices, MSRP, discount schedules
  - Currency Conversion: use pricing_convert for FX conversion (never calculate manually)
  - Config Management: update FX rates, VAT percentages via pricing_config_update

  If user_instructions are present, follow them strictly — they contain item lists and pricing rules.

  Results from pricing_convert render automatically in the frontend table.
  Do NOT list individual prices in your response.
rag_partitions:
  - pricing/margin-rules
  - pricing/tariff-schedules
```

- [ ] **Step 3: Create reconciliation.yaml**

Extract from `agents/prompts/recon_agent.md` and `agents/configs/recon_agent.yaml`:

```yaml
profile_id: reconciliation
display_name: "Reconciliation"
trigger_tools:
  - recon_run
  - recon_get_exceptions
  - recon_get_evidence
  - recon_approve_match
prompt_fragment: |
  ## Reconciliation Context

  You can run Stripe-to-NetSuite reconciliation, investigate exceptions, and guide month-end close.

  Workflow:
  1. recon_run: Run matching for a date range. Report: matched count, exception count, match rate, variance.
  2. recon_get_exceptions: Fetch unmatched/low-confidence results. Present as table.
  3. Investigate: Use netsuite_suiteql to query related transactions near the date/amount.
  4. recon_approve_match: Approve a match (requires user confirmation via HITL).
  5. recon_get_evidence: Export evidence pack for a run.

  Variance types: fees, fx_rounding, timing, missing, duplicate, chargeback, manual_adjustment.
  All approvals require explicit user confirmation. Never auto-approve.
rag_partitions:
  - recon/matching-rules
  - recon/variance-taxonomy
```

- [ ] **Step 4: Create netsuite_writes.yaml**

```yaml
profile_id: netsuite_writes
display_name: "NetSuite Write Operations"
trigger_tools:
  - "ext__*__ns_createRecord"
  - "ext__*__ns_updateRecord"
prompt_fragment: |
  ## NetSuite Write Operations

  You can create and update NetSuite records. ALL writes are intercepted for user confirmation.
  For updates: explain what will change and why.
  For creates: list the fields you'll set and why.
  After calling a write tool, you'll see "confirmation_required".
  Summarize the proposal and wait for user approval.
  NEVER claim a write succeeded until the user explicitly approves.
rag_partitions: []
```

- [ ] **Step 5: Verify profiles load**

Run: `cd backend && .venv/bin/python -c "from app.services.chat.knowledge_profiles import load_all_profiles; ps = load_all_profiles(); print(f'{len(ps)} profiles loaded: {[p.profile_id for p in ps]}')"`
Expected: `4 profiles loaded: ['bigquery', 'netsuite_writes', 'pricing', 'reconciliation']`

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/chat/knowledge_profiles/*.yaml
git commit -m "feat(agent): extract domain knowledge from specialist agents into YAML profiles"
```

---

### Task 3: Prompt Assembler + Tests

**Files:**
- Create: `backend/app/services/chat/prompt_assembler.py`
- Test: `backend/tests/test_prompt_assembler.py`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_prompt_assembler.py
import pytest
from app.services.chat.knowledge_profiles.loader import KnowledgeProfile
from app.services.chat.prompt_assembler import (
    assemble_knowledge_context,
    build_disambiguation_instruction,
    get_active_profiles,
    build_source_pin_hint,
)


_BQ_PROFILE = KnowledgeProfile(
    profile_id="bigquery",
    display_name="BigQuery Analytics",
    trigger_tools=["bigquery_sql", "bigquery_schema"],
    prompt_fragment="## BigQuery Context\nUse LIMIT N.",
    rag_partitions=["bi/schema-docs"],
)
_PRICING_PROFILE = KnowledgeProfile(
    profile_id="pricing",
    display_name="Pricing Engine",
    trigger_tools=["pricing_convert"],
    prompt_fragment="## Pricing Context\nUse pricing_convert.",
    rag_partitions=["pricing/margin-rules"],
)


class TestGetActiveProfiles:
    def test_matches_single(self):
        result = get_active_profiles(
            [_BQ_PROFILE, _PRICING_PROFILE],
            {"bigquery_sql", "netsuite_suiteql"},
        )
        assert len(result) == 1
        assert result[0].profile_id == "bigquery"

    def test_matches_multiple(self):
        result = get_active_profiles(
            [_BQ_PROFILE, _PRICING_PROFILE],
            {"bigquery_sql", "pricing_convert", "netsuite_suiteql"},
        )
        assert len(result) == 2

    def test_matches_none(self):
        result = get_active_profiles(
            [_BQ_PROFILE, _PRICING_PROFILE],
            {"netsuite_suiteql", "rag_search"},
        )
        assert result == []


class TestAssembleKnowledgeContext:
    def test_single_profile_injects_fragment(self):
        result = assemble_knowledge_context([_BQ_PROFILE])
        assert "## BigQuery Context" in result
        assert "LIMIT N" in result

    def test_multiple_profiles_inject_all(self):
        result = assemble_knowledge_context([_BQ_PROFILE, _PRICING_PROFILE])
        assert "## BigQuery Context" in result
        assert "## Pricing Context" in result

    def test_empty_profiles(self):
        result = assemble_knowledge_context([])
        assert result == ""


class TestDisambiguationInstruction:
    def test_injected_when_multiple(self):
        result = build_disambiguation_instruction([_BQ_PROFILE, _PRICING_PROFILE])
        assert "Multiple Data Sources" in result

    def test_not_injected_when_single(self):
        result = build_disambiguation_instruction([_BQ_PROFILE])
        assert result == ""

    def test_not_injected_when_empty(self):
        result = build_disambiguation_instruction([])
        assert result == ""


class TestSourcePinHint:
    def test_bigquery_pin(self):
        result = build_source_pin_hint("bigquery")
        assert "BigQuery" in result

    def test_netsuite_pin(self):
        result = build_source_pin_hint("netsuite")
        assert "NetSuite" in result

    def test_no_pin(self):
        result = build_source_pin_hint(None)
        assert result == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/python -m pytest tests/test_prompt_assembler.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement the prompt assembler**

```python
# backend/app/services/chat/prompt_assembler.py
"""Prompt assembler — composes the final system prompt from base template,
knowledge profiles, and tenant context.

Replaces the three-tier routing system. Instead of routing queries to
specialized agents, this injects domain-specific context into the unified
agent's prompt based on which tools are available.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.chat.knowledge_profiles.loader import KnowledgeProfile


DISAMBIGUATION_INSTRUCTION = """

## Multiple Data Sources Available
You have access to multiple data sources. Choose the best source based on the query:
- NetSuite: transactional data (orders, invoices, customers, inventory, financial reports)
- BigQuery: analytics, marketing, aggregated metrics, third-party data

If the query could be answered by multiple sources, use the most authoritative one.
If genuinely unsure, ask the user: "I can check this in [source A] or [source B]. Which would you prefer, or should I check both?"
"""


def get_active_profiles(
    profiles: list[KnowledgeProfile],
    tool_names: set[str],
) -> list[KnowledgeProfile]:
    """Return profiles whose trigger tools match the available tool set."""
    return [p for p in profiles if p.matches_tools(tool_names)]


def assemble_knowledge_context(active_profiles: list[KnowledgeProfile]) -> str:
    """Build the knowledge context string from active profiles."""
    if not active_profiles:
        return ""
    parts = []
    for profile in active_profiles:
        if profile.prompt_fragment.strip():
            parts.append(profile.prompt_fragment.rstrip())
    return "\n\n".join(parts) if parts else ""


def build_disambiguation_instruction(active_profiles: list[KnowledgeProfile]) -> str:
    """Return disambiguation instruction when multiple data sources are active."""
    if len(active_profiles) < 2:
        return ""
    return DISAMBIGUATION_INSTRUCTION


def build_source_pin_hint(source_pin: str | None) -> str:
    """Build a lightweight prompt hint for session source affinity."""
    if not source_pin:
        return ""
    source_name = {"bigquery": "BigQuery", "netsuite": "NetSuite"}.get(source_pin, source_pin)
    return (
        f"\n\n## Session Context\n"
        f"Previous queries in this session used {source_name}. "
        f"For follow-up questions, prefer {source_name} unless the query "
        f"clearly belongs to a different source."
    )


def collect_rag_partitions(active_profiles: list[KnowledgeProfile]) -> list[str]:
    """Collect all RAG partition IDs from active profiles for batched retrieval."""
    partitions: list[str] = []
    for profile in active_profiles:
        partitions.extend(profile.rag_partitions)
    return partitions
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/python -m pytest tests/test_prompt_assembler.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/chat/prompt_assembler.py backend/tests/test_prompt_assembler.py
git commit -m "feat(agent): add prompt assembler — knowledge context, disambiguation, source pin hints"
```

---

### Task 4: Expand Orchestrator Path Tests (Safety Net)

**Files:**
- Modify: `backend/tests/test_orchestrator_paths.py`

This task MUST be completed BEFORE any orchestrator deletion (Task 6). These tests verify that no code path after deletion produces an UnboundLocalError.

- [ ] **Step 1: Read existing test file and add new path tests**

Add tests covering every branch point that will change during routing deletion. The tests should mock `run_chat_turn` and verify that variables used after if/elif chains are initialized on every path.

New paths to cover:
1. `write_confirm` approve path — variables set correctly
2. `write_confirm` reject path — variables set correctly
3. Unified agent path WITHOUT routing (no `_selected_agent_id` set)
4. Importance tier CASUAL — context assembly skipped
5. Post-deletion: no `_is_chitchat` variable — replaced by importance tier check

- [ ] **Step 2: Run to verify existing + new tests pass**

Run: `cd backend && .venv/bin/python -m pytest tests/test_orchestrator_paths.py -v`
Expected: All PASS (existing 4 + new tests)

- [ ] **Step 3: Commit**

```bash
git add backend/tests/test_orchestrator_paths.py
git commit -m "test(orchestrator): expand path coverage for routing deletion safety"
```

---

### Task 5: Add Batched RAG Partition Retrieval

**Files:**
- Modify: `backend/app/services/chat/domain_knowledge.py`

Add a `partition_ids` parameter to `retrieve_domain_knowledge()` so the assembler can request chunks from specific partitions in a single query.

- [ ] **Step 1: Write the failing test**

```python
# In backend/tests/test_domain_knowledge.py (or create if needed)
# Test that partition_ids filter is applied to the SQL query
```

- [ ] **Step 2: Modify retrieve_domain_knowledge**

Add `partition_ids: list[str] | None = None` parameter. When provided, add a WHERE clause: `DomainKnowledgeChunk.partition_id.in_(partition_ids)`. This is a single DB query regardless of how many partitions.

- [ ] **Step 3: Run tests**

Run: `cd backend && .venv/bin/python -m pytest tests/test_domain_knowledge.py -v`

- [ ] **Step 4: Commit**

```bash
git add backend/app/services/chat/domain_knowledge.py
git commit -m "feat(rag): add partition_ids filter to retrieve_domain_knowledge for batched profile retrieval"
```

---

### Task 6: Orchestrator — Replace Routing with Knowledge Profiles

This is the critical surgery task. Touches `orchestrator.py` (2,802 lines). Work carefully.

**Files:**
- Modify: `backend/app/services/chat/orchestrator.py`

**What to delete from orchestrator.py:**
- `_select_agent()` function and all veto phrase constants (~200 lines)
- `_CHITCHAT_RE` regex and chitchat short-circuit block
- Source picker block (lines ~1168-1250)
- Three-tier routing block (lines ~1905-1962)
- Agent instantiation fork (`if _selected_agent_id:` ... `else: UnifiedAgent(...)`)
- Source pin routing logic (read/write in routing context — keep the post-turn auto-update)
- Legacy multi-agent coordinator path (`if not use_unified:` block)
- All imports of deleted modules (RuleRouter, SemanticRouter, AgentRegistry, etc.)

**What to add:**
- Load knowledge profiles at module init (like `_agent_registry` was)
- In the unified agent path: call `get_active_profiles()` + `assemble_knowledge_context()` + `collect_rag_partitions()`
- Replace chitchat check with `importance_tier.value <= 1` for context gating
- Inject `build_source_pin_hint()` into system prompt when pin exists
- Always use `UnifiedAgent` (no more instantiation fork)

- [ ] **Step 1: Delete routing imports and constants at the top of the file**

Remove imports of: `AgentRegistry`, `RuleRouter`, `SemanticRouter`, `_agent_registry`, `_TOOL_TO_AGENT`, `_infer_previous_agent`, `_FINANCIAL_VETO_PHRASES`, `_is_financial_query`, `_NETSUITE_VETO_PHRASES`, `_is_netsuite_operational_query`, `_CHITCHAT_RE`.

Add imports of: `load_all_profiles`, `get_active_profiles`, `assemble_knowledge_context`, `build_disambiguation_instruction`, `build_source_pin_hint`, `collect_rag_partitions`.

- [ ] **Step 2: Replace module-level agent registry with profile loading**

Replace:
```python
_agent_registry = AgentRegistry()
try:
    _agent_registry.load_configs(_configs_dir)
except Exception:
    pass
```

With:
```python
from app.services.chat.knowledge_profiles import load_all_profiles
_knowledge_profiles = load_all_profiles()
```

- [ ] **Step 3: Replace chitchat short-circuit with importance_tier gating**

Replace the `_is_chitchat = bool(_CHITCHAT_RE.match(...))` block with importance tier check. In the unified agent path, where `_is_chitchat` was checked:

```python
_is_casual = importance_tier.value <= 1  # CASUAL tier
if _is_casual:
    context_need = ContextNeed.DOCS
    context = {"user_timezone": user_timezone}
    # ... skip expensive context assembly
```

- [ ] **Step 4: Replace three-tier routing with profile-based context injection**

Delete the entire `_select_agent()` call and the routing block. Replace with:

```python
# Knowledge profile matching — inject domain context based on available tools
from app.services.chat.prompt_assembler import (
    get_active_profiles, assemble_knowledge_context,
    build_disambiguation_instruction, build_source_pin_hint,
    collect_rag_partitions,
)

tool_names = {t["name"] for t in tool_definitions}
active_profiles = get_active_profiles(_knowledge_profiles, tool_names)

# Inject profile knowledge into system prompt
knowledge_context = assemble_knowledge_context(active_profiles)
if knowledge_context:
    system_prompt += f"\n\n{knowledge_context}"
disambiguation = build_disambiguation_instruction(active_profiles)
if disambiguation:
    system_prompt += disambiguation
pin_hint = build_source_pin_hint(getattr(session, "source_pin", None))
if pin_hint:
    system_prompt += pin_hint

# Batched RAG retrieval for active profile partitions
profile_partitions = collect_rag_partitions(active_profiles)
```

- [ ] **Step 5: Always use UnifiedAgent (delete instantiation fork)**

Replace the `if _selected_agent_id: ... else: UnifiedAgent(...)` block with just:

```python
unified_agent = UnifiedAgent(
    tenant_id=tenant_id,
    user_id=user_id,
    correlation_id=correlation_id,
    metadata=metadata if _need_schemas else None,
    policy=active_policy,
    context_need=context_need,
)
```

- [ ] **Step 6: Delete legacy coordinator path**

Remove the entire `if not use_unified:` block (~150 lines) that creates `MultiAgentCoordinator`. Remove the `use_unified` flag check. Remove the `coordinator.py` import.

- [ ] **Step 7: Run path tests to verify no UnboundLocalError**

Run: `cd backend && .venv/bin/python -m pytest tests/test_orchestrator_paths.py -v`
Expected: All PASS

- [ ] **Step 8: Run full test suite**

Run: `cd backend && .venv/bin/python -m pytest -x -q --ignore=tests/test_bi_agent_end_to_end.py --ignore=tests/test_financial_routing.py`
Expected: PASS (some routing-specific tests will need deletion in Task 8)

- [ ] **Step 9: Commit**

```bash
git add backend/app/services/chat/orchestrator.py
git commit -m "refactor(agent): replace three-tier routing with knowledge profile injection in orchestrator"
```

---

### Task 7: Delete Routing Files

**Files:**
- Delete: `backend/app/services/chat/routing/` (entire directory)
- Delete: `backend/app/services/chat/source_picker.py`
- Delete: `backend/app/services/chat/coordinator.py`
- Delete: `backend/app/services/chat/agents/agent_registry.py`
- Delete: `backend/app/services/chat/agents/specialized_agent.py`
- Delete: `backend/app/services/chat/agents/tool_filter.py`
- Delete: `backend/app/services/chat/agents/configs/bi_agent.yaml`
- Delete: `backend/app/services/chat/agents/configs/pricing_agent.yaml`
- Delete: `backend/app/services/chat/agents/configs/recon_agent.yaml`
- Delete: `backend/app/services/chat/agents/prompts/bi_agent_prompt.md`
- Delete: `backend/app/services/chat/agents/prompts/pricing_agent_prompt.md`
- Delete: `backend/app/services/chat/agents/prompts/recon_agent.md`

- [ ] **Step 1: Delete all routing files**

```bash
rm -rf backend/app/services/chat/routing/
rm backend/app/services/chat/source_picker.py
rm backend/app/services/chat/coordinator.py
rm backend/app/services/chat/agents/agent_registry.py
rm backend/app/services/chat/agents/specialized_agent.py
rm backend/app/services/chat/agents/tool_filter.py
rm backend/app/services/chat/agents/configs/bi_agent.yaml
rm backend/app/services/chat/agents/configs/pricing_agent.yaml
rm backend/app/services/chat/agents/configs/recon_agent.yaml
rm backend/app/services/chat/agents/prompts/bi_agent_prompt.md
rm backend/app/services/chat/agents/prompts/pricing_agent_prompt.md
rm backend/app/services/chat/agents/prompts/recon_agent.md
```

- [ ] **Step 2: Remove stale imports across codebase**

Grep for imports of deleted modules and remove them:
```bash
grep -rn "from app.services.chat.routing" backend/app/
grep -rn "from app.services.chat.source_picker" backend/app/
grep -rn "from app.services.chat.coordinator" backend/app/
grep -rn "from app.services.chat.agents.agent_registry" backend/app/
grep -rn "from app.services.chat.agents.specialized_agent" backend/app/
grep -rn "from app.services.chat.agents.tool_filter" backend/app/
```

- [ ] **Step 3: Verify no import errors**

Run: `cd backend && .venv/bin/python -c "from app.services.chat.orchestrator import run_chat_turn; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor(agent): delete routing files, coordinator, agent registry, specialist configs/prompts"
```

---

### Task 8: Delete and Update Routing Tests

**Files:**
- Delete: `backend/tests/test_rule_router.py`
- Delete: `backend/tests/test_semantic_router.py`
- Delete: `backend/tests/test_semantic_router_history.py`
- Delete: `backend/tests/test_source_picker.py`
- Delete: `backend/tests/test_source_picker_integration.py`
- Delete: `backend/tests/test_agent_registry.py`
- Delete: `backend/tests/test_agent_yaml_configs.py`
- Delete: `backend/tests/test_orchestrator_routing.py`
- Delete: `backend/tests/test_session_agent_pinning.py`
- Delete: `backend/tests/test_end_to_end_agent_framework.py`
- Delete: `backend/tests/test_bi_agent_end_to_end.py`
- Delete: `backend/tests/test_financial_routing.py`
- Delete: `backend/tests/test_connection_aware_orchestrator.py` (if exists)

- [ ] **Step 1: Delete routing test files**

```bash
rm -f backend/tests/test_rule_router.py
rm -f backend/tests/test_semantic_router.py
rm -f backend/tests/test_semantic_router_history.py
rm -f backend/tests/test_source_picker.py
rm -f backend/tests/test_source_picker_integration.py
rm -f backend/tests/test_agent_registry.py
rm -f backend/tests/test_agent_yaml_configs.py
rm -f backend/tests/test_orchestrator_routing.py
rm -f backend/tests/test_session_agent_pinning.py
rm -f backend/tests/test_end_to_end_agent_framework.py
rm -f backend/tests/test_bi_agent_end_to_end.py
rm -f backend/tests/test_financial_routing.py
rm -f backend/tests/test_connection_aware_orchestrator.py
```

- [ ] **Step 2: Run full backend test suite**

Run: `cd backend && .venv/bin/python -m pytest -x -q`
Expected: All remaining tests PASS

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "test(agent): remove routing test files — replaced by knowledge profile and assembler tests"
```

---

### Task 9: Frontend — Remove Source Picker

**Files:**
- Delete: `frontend/src/components/chat/source-picker-card.tsx`
- Delete: `frontend/src/components/chat/__tests__/source-picker-card.test.tsx`
- Modify: `frontend/src/components/chat/message-list.tsx` (remove source picker rendering)
- Modify: `frontend/src/app/(dashboard)/chat/page.tsx` (remove handleSourcePick, activeSourcePick)
- Modify: `frontend/src/lib/types.ts` (remove SourcePickerData, SourcePickerOption)
- Modify: `frontend/src/lib/chat-stream.ts` (remove source_picker from StreamBlock if present)

- [ ] **Step 1: Delete source picker files**

```bash
rm frontend/src/components/chat/source-picker-card.tsx
rm frontend/src/components/chat/__tests__/source-picker-card.test.tsx
```

- [ ] **Step 2: Remove source picker from message-list.tsx**

Delete the `if (structuredOutput?.type === "source_picker")` early return block and the `SourcePickerCard` import.

- [ ] **Step 3: Remove source picker from page.tsx**

Delete `handleSourcePick`, `activeSourcePick` state, `onSourcePick` prop passing, and the `source_pick` option from `handleSend` opts type (keep `write_confirm`).

- [ ] **Step 4: Clean up types.ts**

Remove `SourcePickerOption` and `SourcePickerData` interfaces.

- [ ] **Step 5: Run frontend tests**

Run: `cd frontend && npx vitest run`
Expected: All remaining tests PASS (source picker tests deleted)

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor(frontend): remove source picker card, types, and page handling"
```

---

### Task 10: Run Benchmark Suite — Regression Gate

This is the go/no-go gate for the entire refactor. The benchmark must match or beat 16 wins out of 18 cases.

- [ ] **Step 1: Run the benchmark**

Run: `cd backend && .venv/bin/python -m app.services.benchmarks.run_vs_mcp --suite sales --tenant-id ce3dfaad-626f-4992-84e9-500c8291ca0a`

Expected: 16+ wins (current baseline is 16-0-2)

- [ ] **Step 2: If benchmark passes, commit and celebrate**

```bash
git commit --allow-empty -m "benchmark: knowledge-driven agent passes regression gate (16+ wins vs Claude+MCP)"
```

- [ ] **Step 3: If benchmark regresses, investigate**

Check which cases regressed. The most likely cause: missing domain knowledge that was in the specialist prompt but not extracted to the knowledge profile. Fix by updating the relevant YAML profile's `prompt_fragment`.

---

### Task 11: Final Verification

- [ ] **Step 1: Run full backend tests**

Run: `cd backend && .venv/bin/python -m pytest -x -q`
Expected: All PASS

- [ ] **Step 2: Run full frontend tests**

Run: `cd frontend && npx vitest run`
Expected: All PASS

- [ ] **Step 3: Run prompt tool sync invariant**

Run: `cd backend && .venv/bin/python -m pytest tests/test_prompt_tool_sync.py -v`
Expected: All PASS

- [ ] **Step 4: Count lines deleted vs added**

```bash
git diff main --stat | tail -1
```
Expected: net deletion of ~1,000+ lines

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit -m "refactor(agent): knowledge-driven unified agent — complete routing deletion"
```
