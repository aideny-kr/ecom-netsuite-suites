# Restore NetSuite Knowledge — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore correct first-attempt SuiteQL on Framework's broken shipping-country queries through three code-only changes — no DB writes, no migrations, no infrastructure.

**Architecture:** Two layers of defense. (1) A universal NetSuite ADDRESS TABLES rule appended to the unified agent system prompt — always-on for any tenant, gives the agent the correct join even when no pattern retrieves. (2) A pattern-retrieval gate fix in `orchestrator.py` so seeded patterns can't be silently skipped by the context classifier on FULL queries when SuiteQL/BigQuery tools are present. (3) A disambiguation precedence clause so explicit user source naming ("in NetSuite") wins over the both-source fallback.

All three changes live in the existing unified-agent + knowledge-profile architecture. No new abstractions, no new files in `app/`, no DB writes.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 async, pytest async. All work is backend-only.

**Spec:** `docs/superpowers/specs/2026-04-16-restore-netsuite-knowledge-design.md` (commit `46cc5a1`).

**Worktree:** `.worktrees/fix-restore-netsuite-knowledge-phase-1/` on branch `fix/restore-netsuite-knowledge-phase-1` (created from `feat/cross-source-sheets` HEAD).

---

## File Map

| File | Op | What |
|---|---|---|
| `backend/app/services/chat/agents/unified_agent.py` | Modify | Insert `ADDRESS TABLES` block in `_SYSTEM_PROMPT` after `CUSTOM LIST FIELDS` (~line 207) |
| `backend/app/services/chat/prompt_assembler.py` | Modify | Add `PRECEDENCE` clause as first rule in `DISAMBIGUATION_INSTRUCTION` (lines 9-20) |
| `backend/app/services/chat/orchestrator.py` | Modify | Extract `_compute_need_patterns()` helper; replace inline gate at line 1387; update injection-matrix comment at lines 1374-1383 |
| `backend/tests/test_unified_agent_suiteql_rules.py` | Create | Asserts `ADDRESS TABLES` block content in `_SYSTEM_PROMPT` |
| `backend/tests/test_prompt_assembler.py` | Modify | Add `TestDisambiguationPrecedence` test class |
| `backend/tests/test_pattern_gate.py` | Create | Unit-tests `_compute_need_patterns()` across context-need × tool-name combinations |

**Why a new helper for `_need_patterns`?** It's a one-liner today, but adding tool-set logic makes it nontrivial. Extracting into a pure function (`context_need + tool_names → bool`) is the cheapest way to test the change in isolation without mocking the orchestrator pipeline. YAGNI: don't extract anything else, just this.

**No frontend changes.** No DB migrations. No env vars. No new dependencies.

**Working directory:** All commands run from `/Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/fix-restore-netsuite-knowledge-phase-1/`. Backend venv is symlinked to main checkout's `.venv`.

---

## Task 1: ADDRESS TABLES block in unified_agent.py

**Files:**
- Modify: `backend/app/services/chat/agents/unified_agent.py` (insert after line 207, the end of `CUSTOM LIST FIELDS`)
- Test: `backend/tests/test_unified_agent_suiteql_rules.py` (NEW)

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_unified_agent_suiteql_rules.py`:

```python
"""Regression tests for SuiteQL dialect rules embedded in the unified agent prompt.

These tests guard against rule loss during prompt edits. Each rule is here because
its absence caused a documented production failure. Do not delete tests when
"cleaning up" — read the docstring of the failing test for the incident context.
"""

from app.services.chat.agents.unified_agent import _SYSTEM_PROMPT


class TestAddressTablesRule:
    """Verify the ADDRESS TABLES block teaches the canonical shipping/billing
    address join pattern.

    Incident: 2026-04-16 staging session burned 10+ tool calls rediscovering
    `transactionShippingAddress` join key. Agent kept trying `sa.recordOwner = t.id`
    and `sa.transaction = t.id` (both wrong). Correct join is `sa.nKey = t.shippingAddress`.
    See spec docs/superpowers/specs/2026-04-16-restore-netsuite-knowledge-design.md.
    """

    def test_block_header_present(self):
        assert "ADDRESS TABLES" in _SYSTEM_PROMPT

    def test_correct_shipping_join_taught(self):
        assert "transactionShippingAddress" in _SYSTEM_PROMPT
        assert "sa.nKey = t.shippingAddress" in _SYSTEM_PROMPT

    def test_correct_billing_join_taught(self):
        assert "transactionBillingAddress" in _SYSTEM_PROMPT
        assert "ba.nKey = t.billingAddress" in _SYSTEM_PROMPT

    def test_wrong_join_keys_explicitly_warned(self):
        # The three wrong keys the agent kept trying in the 2026-04-16 staging session
        assert "sa.recordOwner = t.id" in _SYSTEM_PROMPT
        assert "sa.transaction = t.id" in _SYSTEM_PROMPT
        assert "sa.id = t.shippingAddress" in _SYSTEM_PROMPT

    def test_country_resolution_taught(self):
        # Both readable display name and raw ISO code are valid
        assert "BUILTIN.DF(sa.country)" in _SYSTEM_PROMPT

    def test_custbody_country_warning_present(self):
        # The custbody fallback was the agent's wrong escape hatch
        assert "custbody" in _SYSTEM_PROMPT.lower()
        # Locate the warning specifically in the address-tables area
        block_start = _SYSTEM_PROMPT.find("ADDRESS TABLES")
        block_end = _SYSTEM_PROMPT.find("TRANSACTION NUMBER CONVENTIONS", block_start)
        block = _SYSTEM_PROMPT[block_start:block_end]
        assert "custom body" in block.lower() or "custbody" in block.lower()

    def test_block_placed_after_custom_list_fields(self):
        # Locality matters: the block sits with related field-resolution rules
        custom_list_pos = _SYSTEM_PROMPT.find("CUSTOM LIST FIELDS")
        address_pos = _SYSTEM_PROMPT.find("ADDRESS TABLES")
        txn_num_pos = _SYSTEM_PROMPT.find("TRANSACTION NUMBER CONVENTIONS")
        assert custom_list_pos < address_pos < txn_num_pos
```

- [ ] **Step 2: Run test to verify it fails**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_unified_agent_suiteql_rules.py -v`

Expected: 7 FAILs with `AssertionError: assert 'ADDRESS TABLES' in '...'` (the block doesn't exist yet).

- [ ] **Step 3: Implement — insert ADDRESS TABLES block in unified_agent.py**

Open `backend/app/services/chat/agents/unified_agent.py`. Find the lines:

```python
# Prevents: filtering custom list fields by string instead of ID (2025)
CUSTOM LIST FIELDS:
- SELECT-type fields store integer IDs. Filter: `WHERE field = <id>` (fastest) or `BUILTIN.DF(field) = 'Value Name'` (readable).
- ID → name mappings in tenant schema Custom List Values. Linkage shown as `(SELECT → customlist_name)`.

TRANSACTION NUMBER CONVENTIONS:
```

Insert the new block between `CUSTOM LIST FIELDS` and `TRANSACTION NUMBER CONVENTIONS`. Use the Edit tool with `old_string` = the `CUSTOM LIST FIELDS` block (verbatim, including the prefix comment) followed by the blank line and `TRANSACTION NUMBER CONVENTIONS:` line; `new_string` = the same thing but with the new block sandwiched in. Concretely:

`old_string`:

```
# Prevents: filtering custom list fields by string instead of ID (2025)
CUSTOM LIST FIELDS:
- SELECT-type fields store integer IDs. Filter: `WHERE field = <id>` (fastest) or `BUILTIN.DF(field) = 'Value Name'` (readable).
- ID → name mappings in tenant schema Custom List Values. Linkage shown as `(SELECT → customlist_name)`.

TRANSACTION NUMBER CONVENTIONS:
```

`new_string`:

```
# Prevents: filtering custom list fields by string instead of ID (2025)
CUSTOM LIST FIELDS:
- SELECT-type fields store integer IDs. Filter: `WHERE field = <id>` (fastest) or `BUILTIN.DF(field) = 'Value Name'` (readable).
- ID → name mappings in tenant schema Custom List Values. Linkage shown as `(SELECT → customlist_name)`.

# Prevents: shipping-country join rediscovery loop (2026-04-09 / 2026-04-16)
ADDRESS TABLES — shipping/billing country, state, city:
- Country lives on the address record, NOT the transaction header. Join: `JOIN transactionShippingAddress sa ON sa.nKey = t.shippingAddress` (or `JOIN transactionBillingAddress ba ON ba.nKey = t.billingAddress`).
- The join key is `sa.nKey = t.shippingAddress`. NEVER `sa.recordOwner = t.id`, NEVER `sa.transaction = t.id`, NEVER `sa.id = t.shippingAddress`.
- Read `BUILTIN.DF(sa.country)` for country name ("Switzerland") or `sa.country` for 2-letter ISO code ("CH"). Both work; prefer `BUILTIN.DF` for display, raw code for filtering.
- Do NOT use custom body fields (`custbody*_ship_country*`, `custbody*_country*`) for country queries unless the user explicitly asks for the custom field. The standard address join is the source of truth.

TRANSACTION NUMBER CONVENTIONS:
```

- [ ] **Step 4: Run test to verify it passes**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_unified_agent_suiteql_rules.py -v`

Expected: 7 PASS.

- [ ] **Step 5: Run sibling prompt tests to confirm no regression**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_prompt_tool_sync.py backend/tests/test_unified_agent_workflow.py backend/tests/test_unified_agent_tool_inventory.py backend/tests/test_system_prompt_placeholder.py -v`

Expected: all PASS. The new block adds content but doesn't change tool inventory or placeholders.

- [ ] **Step 6: Commit**

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/fix-restore-netsuite-knowledge-phase-1
git add backend/app/services/chat/agents/unified_agent.py backend/tests/test_unified_agent_suiteql_rules.py
git commit -m "fix(chat): teach unified agent the transactionShippingAddress join

Adds ADDRESS TABLES block to the SuiteQL dialect rules in _SYSTEM_PROMPT.
Lives next to TEXT RESOLUTION and CUSTOM LIST FIELDS — the related
field-resolution quirks. Covers both shipping and billing addresses with
the canonical sa.nKey = t.shippingAddress join, the three wrong keys the
agent kept trying (recordOwner, transaction, id), country name vs ISO
code resolution, and an explicit warning against custbody* fallbacks.

NetSuite platform quirk — applies to every tenant. In Phase 2 this block
moves into knowledge_profiles/netsuite.yaml along with the rest of the
SuiteQL dialect rules.

Regression test: tests/test_unified_agent_suiteql_rules.py::TestAddressTablesRule.

Spec: docs/superpowers/specs/2026-04-16-restore-netsuite-knowledge-design.md"
```

---

## Task 2: PRECEDENCE clause in DISAMBIGUATION_INSTRUCTION

**Files:**
- Modify: `backend/app/services/chat/prompt_assembler.py` (lines 9-20: `DISAMBIGUATION_INSTRUCTION` constant)
- Test: `backend/tests/test_prompt_assembler.py` (extend with new `TestDisambiguationPrecedence` class)

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_prompt_assembler.py` (do not delete or modify existing tests). Find the `TestDisambiguationInstruction` class and add the new class right after it:

```python
class TestDisambiguationPrecedence:
    """The disambiguation prompt MUST honor explicit user source naming.

    Incident: 2026-04-16 staging — user typed "Can you look from NetSuite?"
    after the agent silently chose BigQuery on the prior turn. The existing
    rule "use the most authoritative one" gave the agent latitude to override
    the user's explicit naming. The PRECEDENCE clause closes that hole.
    """

    def test_precedence_clause_present(self):
        result = build_disambiguation_instruction([_BQ_PROFILE, _PRICING_PROFILE])
        assert "PRECEDENCE" in result

    def test_explicit_naming_examples(self):
        result = build_disambiguation_instruction([_BQ_PROFILE, _PRICING_PROFILE])
        # Common phrasings the user might type
        assert "in NetSuite" in result
        assert "from BigQuery" in result

    def test_use_only_clause_present(self):
        result = build_disambiguation_instruction([_BQ_PROFILE, _PRICING_PROFILE])
        # The hard rule: ONLY the named source
        assert "use ONLY that source" in result

    def test_both_source_escape_hatch_documented(self):
        result = build_disambiguation_instruction([_BQ_PROFILE, _PRICING_PROFILE])
        # The override-the-override: explicit "compare" or "and also" still triggers both
        assert "compare" in result.lower() or "both" in result.lower()

    def test_precedence_appears_before_authoritative_rule(self):
        """Position matters — PRECEDENCE must come first so the model reads it
        before the existing 'most authoritative' fallback."""
        result = build_disambiguation_instruction([_BQ_PROFILE, _PRICING_PROFILE])
        precedence_pos = result.find("PRECEDENCE")
        authoritative_pos = result.find("most authoritative")
        assert precedence_pos > -1
        assert authoritative_pos > -1
        assert precedence_pos < authoritative_pos

    def test_still_returns_empty_for_single_profile(self):
        # Don't break the existing behavior — only inject when 2+ profiles active
        result = build_disambiguation_instruction([_BQ_PROFILE])
        assert result == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_prompt_assembler.py::TestDisambiguationPrecedence -v`

Expected: 5 FAILs (the empty-single-profile test will pass — that's existing behavior).

- [ ] **Step 3: Implement — update DISAMBIGUATION_INSTRUCTION**

Edit `backend/app/services/chat/prompt_assembler.py`. Replace the `DISAMBIGUATION_INSTRUCTION` constant.

`old_string`:

```python
DISAMBIGUATION_INSTRUCTION = """

## Multiple Data Sources Available
You have access to multiple data sources. Choose the best source based on the query:
- NetSuite: transactional data (orders, invoices, customers, inventory, financial reports)
- BigQuery: analytics, marketing, aggregated metrics, third-party data

If the question clearly requires data from both sources, call both tools and synthesize the results.
Identify the join key (SKU, customer email, order ID, date range) to correlate cross-source data.
If the query can be fully answered by one source, use the most authoritative one.
Only ask the user if you genuinely cannot determine which source(s) to use.
"""
```

`new_string`:

```python
DISAMBIGUATION_INSTRUCTION = """

## Multiple Data Sources Available
You have access to multiple data sources. Choose the best source based on the query:
- NetSuite: transactional data (orders, invoices, customers, inventory, financial reports)
- BigQuery: analytics, marketing, aggregated metrics, third-party data

PRECEDENCE: If the user explicitly names a source ("in NetSuite", "from BigQuery", "check NetSuite for ...", "look it up in BigQuery"), use ONLY that source. Do not call any other source unless the user explicitly asks for both ("compare NetSuite to BigQuery", "and also BigQuery", "side-by-side"). The explicit-naming rule overrides the both-source rule below.

If the question clearly requires data from both sources, call both tools and synthesize the results.
Identify the join key (SKU, customer email, order ID, date range) to correlate cross-source data.
If the query can be fully answered by one source, use the most authoritative one.
Only ask the user if you genuinely cannot determine which source(s) to use.
"""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_prompt_assembler.py -v`

Expected: all tests PASS, including the existing `TestDisambiguationInstruction` class (which only checks "Multiple Data Sources" substring — still present).

- [ ] **Step 5: Commit**

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/fix-restore-netsuite-knowledge-phase-1
git add backend/app/services/chat/prompt_assembler.py backend/tests/test_prompt_assembler.py
git commit -m "fix(chat): honor explicit user source naming in disambiguation

Adds a PRECEDENCE clause to DISAMBIGUATION_INSTRUCTION as the FIRST
rule (before 'use the most authoritative one'). When the user explicitly
names a source ('in NetSuite', 'from BigQuery'), the agent uses ONLY
that source. Override-the-override: 'compare NetSuite to BigQuery' or
'and also BigQuery' still triggers both.

Incident: 2026-04-16 staging — user had to type 'Can you look from
NetSuite?' mid-session after the agent silently chose BigQuery on the
prior turn. The 'most authoritative one' rule gave the model latitude
to override explicit naming.

Spec: docs/superpowers/specs/2026-04-16-restore-netsuite-knowledge-design.md"
```

---

## Task 3: Replace `_need_patterns` gate with tool-presence detection

This task does three things in one change because they're tightly coupled: extract a pure helper for testability, replace the inline gate at `orchestrator.py:1387`, and update the injection-matrix comment at lines 1374-1383 so it stays honest.

**Files:**
- Modify: `backend/app/services/chat/orchestrator.py` (extract helper near line 326 with the other helpers; replace gate at line 1387; update matrix comment at lines 1374-1383)
- Test: `backend/tests/test_pattern_gate.py` (NEW)

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_pattern_gate.py`:

```python
"""Unit tests for `_compute_need_patterns` — the pattern-retrieval gate.

The gate decides whether to retrieve seeded SuiteQL/BigQuery patterns
during context assembly. Pre-2026-04-16 it was tied to ContextNeed.DATA
only; FULL (investigation) queries deliberately skipped patterns. With
admin-seeded high-quality patterns, that's wrong — patterns should
retrieve whenever a SQL query tool is in the available tool set,
regardless of the context-need classifier's choice.

Incident: 2026-04-16 staging — Framework's 6 shipping-country patterns
were never retrieved because the user's question 'what are the 4 new
countries we recently launched?' classified as FULL, and the gate
skipped patterns under FULL by design.
"""

import pytest

from app.services.chat.orchestrator import ContextNeed, _compute_need_patterns


class TestComputeNeedPatterns:
    def test_data_with_suiteql_returns_true(self):
        """Existing DATA behavior is preserved when SuiteQL is connected."""
        assert _compute_need_patterns(ContextNeed.DATA, {"netsuite_suiteql"}) is True

    def test_data_with_bigquery_returns_true(self):
        assert _compute_need_patterns(ContextNeed.DATA, {"bigquery_sql"}) is True

    def test_full_with_suiteql_returns_true(self):
        """The fix: FULL no longer skips patterns when SuiteQL is connected."""
        assert _compute_need_patterns(ContextNeed.FULL, {"netsuite_suiteql"}) is True

    def test_full_with_bigquery_returns_true(self):
        assert _compute_need_patterns(ContextNeed.FULL, {"bigquery_sql"}) is True

    def test_full_with_both_sql_tools_returns_true(self):
        assert _compute_need_patterns(ContextNeed.FULL, {"netsuite_suiteql", "bigquery_sql"}) is True

    def test_full_with_no_sql_tools_returns_false(self):
        """No SQL tool in the toolset → no patterns to retrieve."""
        assert _compute_need_patterns(ContextNeed.FULL, {"web_search", "pricing_convert"}) is False

    def test_data_with_no_sql_tools_returns_false(self):
        """Even DATA shouldn't request patterns when there are no SQL tools."""
        assert _compute_need_patterns(ContextNeed.DATA, {"pricing_convert"}) is False

    def test_ext_mcp_suiteql_tool_returns_true(self):
        """External MCP SuiteQL tool names like ext__abc-123__ns_runCustomSuiteQL match."""
        assert _compute_need_patterns(ContextNeed.FULL, {"ext__abc-123__ns_runCustomSuiteQL"}) is True

    def test_ext_mcp_non_suiteql_tool_returns_false(self):
        """ext__ prefix alone doesn't qualify — must be a SuiteQL variant."""
        assert (
            _compute_need_patterns(ContextNeed.FULL, {"ext__abc-123__ns_runReport"})
            is False
        )

    def test_docs_with_suiteql_returns_true(self):
        """Tool presence wins over context-need across the board."""
        assert _compute_need_patterns(ContextNeed.DOCS, {"netsuite_suiteql"}) is True

    def test_workspace_with_suiteql_returns_true(self):
        assert _compute_need_patterns(ContextNeed.WORKSPACE, {"netsuite_suiteql"}) is True

    def test_financial_with_suiteql_returns_true(self):
        assert _compute_need_patterns(ContextNeed.FINANCIAL, {"netsuite_suiteql"}) is True

    def test_empty_tool_names_returns_false(self):
        assert _compute_need_patterns(ContextNeed.FULL, set()) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_pattern_gate.py -v`

Expected: ImportError or all 13 tests FAIL — `_compute_need_patterns` doesn't exist yet.

- [ ] **Step 3: Implement — extract `_compute_need_patterns` helper**

Open `backend/app/services/chat/orchestrator.py`. Find the `ContextNeed` class and the `_WORKSPACE_RE` regex below it (around line 317-333). Insert the new helper function right after `ContextNeed` so it sits with related code.

Use Edit with:

`old_string`:

```python
class ContextNeed:
    """How much dynamic context to inject into the agent prompt."""

    FULL = "full"  # Custom fields, complex joins — inject everything
    DATA = "data"  # Standard tables, no custom fields — skip onboarding profile
    DOCS = "docs"  # Documentation question — skip schemas, inject RAG only
    WORKSPACE = "workspace"  # Script question — skip all NetSuite schemas
    FINANCIAL = "financial"  # Financial report — inject only vernacular + onboarding


_WORKSPACE_RE = re.compile(
```

`new_string`:

```python
class ContextNeed:
    """How much dynamic context to inject into the agent prompt."""

    FULL = "full"  # Custom fields, complex joins — inject everything
    DATA = "data"  # Standard tables, no custom fields — skip onboarding profile
    DOCS = "docs"  # Documentation question — skip schemas, inject RAG only
    WORKSPACE = "workspace"  # Script question — skip all NetSuite schemas
    FINANCIAL = "financial"  # Financial report — inject only vernacular + onboarding


def _compute_need_patterns(context_need: str, tool_names: set[str]) -> bool:
    """Decide whether to retrieve seeded SuiteQL/BigQuery patterns this turn.

    Pre-2026-04-16 this was `context_need in (ContextNeed.DATA,)` — patterns
    only fired when the classifier said DATA. That stranded admin-seeded
    patterns whenever a query classified as FULL (investigation), which is
    exactly when worked examples are most useful. New rule: patterns retrieve
    whenever a SQL query tool is connected, regardless of context_need.
    """
    if {"netsuite_suiteql", "bigquery_sql"} & tool_names:
        return True
    return any(name.startswith("ext__") and "suiteql" in name.lower() for name in tool_names)


_WORKSPACE_RE = re.compile(
```

- [ ] **Step 4: Run helper unit tests to verify they pass**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_pattern_gate.py -v`

Expected: all 13 PASS.

- [ ] **Step 5: Replace the inline gate in `run_chat_turn`**

Edit `backend/app/services/chat/orchestrator.py`. Find the existing gate computation block (lines 1374-1389).

`old_string`:

```python
                    # Injection matrix:
                    #   Block              FULL  DATA  DOCS  WORKSPACE  FINANCIAL
                    #   tenant_schema       ❌    ✅    ❌      ❌        ❌
                    #   table_schemas       ✅    ✅    ❌      ❌        ❌
                    #   tenant_vernacular   ❌    ✅    ❌      ❌        ✅
                    #   domain_knowledge    ❌    ✅    ✅      ❌        ❌
                    #   onboarding_profile  ❌    ❌    ❌      ❌        ✅
                    #   proven_patterns     ❌    ✅    ❌      ❌        ❌
                    #   learned_rules       ✅    ✅    ✅      ✅        ✅    (always)
                    # FULL = investigation ("why") — minimal context so agent reasons freely

                    _need_vernacular = context_need in (ContextNeed.DATA, ContextNeed.FINANCIAL)
                    _need_domain_knowledge = context_need in (ContextNeed.DATA, ContextNeed.DOCS)
                    _need_patterns = context_need in (ContextNeed.DATA,)
                    _need_schemas = context_need in (ContextNeed.FULL, ContextNeed.DATA)
                    _need_onboarding = context_need in (ContextNeed.FINANCIAL,)
```

`new_string`:

```python
                    # Injection matrix:
                    #   Block              FULL  DATA  DOCS  WORKSPACE  FINANCIAL
                    #   tenant_schema       ❌    ✅    ❌      ❌        ❌
                    #   table_schemas       ✅    ✅    ❌      ❌        ❌
                    #   tenant_vernacular   ❌    ✅    ❌      ❌        ✅
                    #   domain_knowledge    ❌    ✅    ✅      ❌        ❌
                    #   onboarding_profile  ❌    ❌    ❌      ❌        ✅
                    #   proven_patterns    (gate by tool presence — see _compute_need_patterns)
                    #   learned_rules       ✅    ✅    ✅      ✅        ✅    (always)
                    # FULL = investigation ("why") — minimal context so agent reasons freely.
                    # proven_patterns: gated by tool presence (any SuiteQL/BQ tool in the toolset),
                    # NOT by context_need. Admin-seeded patterns are high-quality and shouldn't be
                    # stranded by the FULL classification. See _compute_need_patterns docstring.

                    _need_vernacular = context_need in (ContextNeed.DATA, ContextNeed.FINANCIAL)
                    _need_domain_knowledge = context_need in (ContextNeed.DATA, ContextNeed.DOCS)
                    _need_patterns = _compute_need_patterns(context_need, _tool_names)
                    _need_schemas = context_need in (ContextNeed.FULL, ContextNeed.DATA)
                    _need_onboarding = context_need in (ContextNeed.FINANCIAL,)
```

- [ ] **Step 6: Run orchestrator path tests + full backend pytest scope around chat**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_orchestrator_paths.py backend/tests/test_chat_orchestrator.py backend/tests/test_pattern_gate.py -v`

Expected: all PASS. The gate change is additive — DATA still triggers patterns when SQL tools present, and now FULL does too.

- [ ] **Step 7: Commit**

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/fix-restore-netsuite-knowledge-phase-1
git add backend/app/services/chat/orchestrator.py backend/tests/test_pattern_gate.py
git commit -m "fix(chat): retrieve patterns when SQL tools connected, ignore context_need

Replaces the _need_patterns = (context_need == DATA) gate with a pure
helper _compute_need_patterns(context_need, tool_names) that checks the
available toolset. Patterns retrieve whenever netsuite_suiteql, bigquery_sql,
or any ext__*__ns_runCustomSuiteQL tool is present, regardless of how the
context classifier scored the query.

Incident: 2026-04-16 staging — Framework has 6 shipping-country patterns
in tenant_query_patterns (success_count=5, last_used April 10) but none
retrieved because the user's investigation query classified as FULL,
which the matrix design deliberately skipped patterns for. Worked examples
are most useful precisely when a query is open-ended; the pre-fix matrix
got it backwards.

Trade-off: ~1-2K extra context tokens on FULL queries when SQL tools are
connected. Benefit: admin-seeded high-quality patterns can never be stranded.
Updated the injection-matrix comment in run_chat_turn to reflect the new
gate.

Spec: docs/superpowers/specs/2026-04-16-restore-netsuite-knowledge-design.md"
```

---

## Task 4: Full backend pytest baseline

A defensive sweep before opening the PR — confirms no test outside the three changed surfaces broke. The three commits should be additive only, but the full suite is the authoritative check.

**Files:** none modified — this is a verification step. If something fails that looks unrelated, STOP and investigate before committing or pushing. Do NOT mark "completed" if any test fails.

- [ ] **Step 1: Run the full backend suite**

Run: `cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/fix-restore-netsuite-knowledge-phase-1 && backend/.venv/bin/python -m pytest backend/tests -x --tb=short 2>&1 | tail -40`

Expected: all 2,846+ tests PASS (the new tests from Tasks 1-3 add ~25 to the count). The `-x` stops at first failure for fast feedback.

- [ ] **Step 2: If any failure occurs that's NOT one of our changed files, investigate**

If a test fails that doesn't import from the three files we modified, the failure was likely already present on the parent branch. Check by running the same test on main:

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites
backend/.venv/bin/python -m pytest backend/tests/<failing_test>.py::<failing_method> -v
```

If it fails on main too, document in the PR description as a pre-existing issue (don't fix in this PR — out of scope). If it passes on main but fails in the worktree, our changes broke something — STOP, debug, fix.

- [ ] **Step 3: If everything passes, no commit needed**

This task is verification, not modification. Move on to Task 5.

---

## Task 5: Push branch and open PR

**Files:** none modified.

- [ ] **Step 1: Confirm git state is clean and on the right branch**

Run: `cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/fix-restore-netsuite-knowledge-phase-1 && git status && git log --oneline -5`

Expected: clean working tree, branch `fix/restore-netsuite-knowledge-phase-1`, three new commits on top of `feat/cross-source-sheets` HEAD (the spec commit `46cc5a1`).

- [ ] **Step 2: Push the branch**

Run: `cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/fix-restore-netsuite-knowledge-phase-1 && git push -u origin fix/restore-netsuite-knowledge-phase-1`

Expected: branch pushed to origin; gh detects no existing PR.

- [ ] **Step 3: Also push to framework remote (per CLAUDE.md memory)**

Run: `cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/fix-restore-netsuite-knowledge-phase-1 && git push framework fix/restore-netsuite-knowledge-phase-1`

Expected: branch pushed to framework remote.

- [ ] **Step 4: Open PR against feat/cross-source-sheets**

Use the gh CLI (per CLAUDE.md). Branch base is `feat/cross-source-sheets` since that's where the spec lives and where this stack of work belongs.

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/fix-restore-netsuite-knowledge-phase-1
gh pr create \
  --base feat/cross-source-sheets \
  --head fix/restore-netsuite-knowledge-phase-1 \
  --title "fix(chat): restore NetSuite shipping-country knowledge (phase 1)" \
  --body "$(cat <<'EOF'
## Summary

Phase 1 of the NetSuite knowledge restoration spec. Three code-only changes,
no DB writes.

- **ADDRESS TABLES block** in \`unified_agent.py\` — universal NetSuite
  platform quirk. Teaches the canonical \`transactionShippingAddress\` /
  \`transactionBillingAddress\` join with \`sa.nKey = t.shippingAddress\`,
  warns against three wrong join keys the agent kept rediscovering, and
  bans \`custbody*_country*\` fallbacks.
- **PRECEDENCE clause** in \`prompt_assembler.py\` \`DISAMBIGUATION_INSTRUCTION\` —
  honors explicit user source naming ("in NetSuite") instead of letting
  the model override.
- **Pattern-retrieval gate fix** in \`orchestrator.py\` — patterns retrieve
  whenever a SQL query tool is in the toolset, regardless of the context
  classifier's choice. Was tied to \`ContextNeed.DATA\` only, which stranded
  Framework's 6 admin-seeded shipping-country patterns on FULL (investigation)
  queries.

Spec: \`docs/superpowers/specs/2026-04-16-restore-netsuite-knowledge-design.md\`
(commit 46cc5a1).

## Test plan

- [x] \`test_unified_agent_suiteql_rules.py::TestAddressTablesRule\` — 7 new tests guard the ADDRESS TABLES block
- [x] \`test_prompt_assembler.py::TestDisambiguationPrecedence\` — 6 new tests guard the PRECEDENCE clause
- [x] \`test_pattern_gate.py::TestComputeNeedPatterns\` — 13 new tests guard the gate logic across context-need × tool-name combinations
- [x] Full backend pytest suite passes (2,846+ tests)
- [ ] **Manual smoke on staging** — re-run the broken query "what are the 4 new countries we recently launched?" as Framework. Expected first-attempt SuiteQL contains \`sa.nKey = t.shippingAddress\` and \`BUILTIN.DF(sa.country)\`, no compound status codes (\`'SalesOrd:C'\`), pattern retrieval logs show \`returned≥1\`. Capture before/after screenshots per the spec's "Assignment" section.
EOF
)"
```

Expected: PR URL printed. Save it.

- [ ] **Step 5: Report PR URL**

Output the PR URL to the user. Do not merge — that's a separate decision after staging smoke test.

---

## Self-Review Checklist (run after writing this plan, fix inline)

**Spec coverage:** Each Phase 1 deliverable from the spec maps to a task —
- "ADDRESS TABLES block in unified_agent.py" → Task 1 ✓
- "Disambiguation prompt change" → Task 2 ✓
- "_need_patterns gate replacement" → Task 3 ✓
- "No DB writes required" → confirmed, all tasks are code-only ✓
- "Smoke-test on staging" → in PR test plan as a manual step (correctly excluded from automated tasks since it requires a live staging environment + visual confirmation)
- "Pattern-similarity diagnostic is a runbook step (separate from PR)" → correctly excluded from this plan ✓
- Phase 2 work → correctly out of scope ✓

**Placeholder scan:** No TBDs, no "implement later," every code step contains the actual code. Test files contain full test code, not stubs.

**Type consistency:** `_compute_need_patterns(context_need: str, tool_names: set[str]) -> bool` — used identically in test, helper definition, and call site. `ContextNeed` constants used as strings (matches the existing class definition). `_tool_names` is the existing variable name at `orchestrator.py:1142`, matching what's used in the helper call site at line 1387.

**File path accuracy:** Verified existing line numbers (`unified_agent.py:204-207` for CUSTOM LIST FIELDS, `orchestrator.py:1142` for `_tool_names`, `orchestrator.py:1387` for the gate, `prompt_assembler.py:9-20` for DISAMBIGUATION_INSTRUCTION, `orchestrator.py:317` for ContextNeed). Read against current branch HEAD `46cc5a1`.

---

## Out of Scope (explicit non-tasks)

- Pattern seeding via SQL — the 6 patterns are already in DB (verified 2026-04-16). Phase 1 fixes retrieval, not storage.
- Pattern-similarity diagnostic script — runbook step, not part of the PR. Operator can write later if smoke test fails.
- Tenant learned-rule insert — the country quirk is universal, lives in the prompt, not in `tenant_learned_rules`.
- `netsuite.yaml` knowledge profile — Phase 2 deliverable.
- Golden dataset RAG ingest — Phase 2 deliverable.
- `seed_tenant_patterns.py` admin script — Phase 2 deliverable.
- Frontend changes — none required.
- DB migrations — none required.
- Agent benchmark re-run — happens automatically via CI gate (`agent-benchmark.yml`); no manual step.
