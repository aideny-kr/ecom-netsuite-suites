# Design: Investigation Mode — End-to-End Persistence + Progressive Output

**Date**: 2026-03-21
**Status**: Implemented and deployed to staging (2026-03-21)
**Diagnosis**: `docs/superpowers/specs/2026-03-20-orchestration-diagnosis.md`

---

## Problem

Same model (Sonnet 4.6), same MCP tools, same NetSuite account. Claude + native MCP produces calm, chronological investigative narratives. Our agent produces one-sentence summaries and stops after the first successful tool result.

Root causes (from diagnosis):
1. "ONLY ONE sentence" output constraint prevents narrative analysis
2. Early exit kills investigation after first successful data result
3. Data success nudge pushes model to present immediately instead of continuing
4. 6-step budget too tight for multi-step investigation
5. No systemnote expertise in prompt (Claude has this from training data)
6. Proven patterns injection can poison investigation queries

## Approach

**Conditional investigation mode.** When `context_need == ContextNeed.FULL`, disable the constraints that fight investigation behavior. All other query types (DATA, FINANCIAL, DOCS, WORKSPACE) remain exactly as-is.

Prove it on investigation first, then selectively expand to other modes if it works.

---

## Design

### 1. Flag Flow

No new classification logic. Piggyback on existing `ContextNeed.FULL`:

```
orchestrator._classify_context_need()
  → ContextNeed.FULL
    → unified_agent._context_need = "full"  (already happens)
      → base_agent reads self._context_need  (already set)
        → Guards: max_steps, early_exit, nudge, output format
    → orchestrator._make_tool_interceptor(context_need)  (already happens)
      → Full rows sent to LLM (already works for FULL)
    → orchestrator already skips proven_patterns for FULL (verified)
```

**Fix classification regex** — add investigation-related terms to `_INVESTIGATION_RE`:
```python
# Add to pattern:
r"history|timeline|trace|audit.?trail|what.?happened|how.?long|when.?was"
```

This ensures queries like "give me history" and "what happened to this order" classify as FULL instead of falling through to DATA.

### 2. Code Changes in `base_agent.py` and `unified_agent.py`

**a) Max steps: 10 → 12 for investigation** (`unified_agent.py` line 625)

The override already exists in `unified_agent.py`. Update from 10 to 12:

```python
@property
def max_steps(self) -> int:
    return 12 if self._context_need == "full" else 6
```

**b) Early exit disabled for investigation** (`base_agent.py`)

Current code (line ~856):
```python
if skippable and _has_successful_data_result([result_str]):
    # skip redundant tools...
```

Change to:
```python
if self._context_need != "full" and skippable and _has_successful_data_result([result_str]):
    # skip redundant tools... (existing behavior, unchanged for DATA)
```

Investigation queries never skip tools after first success — the model keeps digging.

**c) Data success nudge disabled for investigation**

Current code (line ~877):
```python
if step >= 1 and _has_successful_data_result(raw_result_strings):
    tool_results_content.append({"type": "text", "text": _DATA_SUCCESS_NUDGE})
```

Change to:
```python
if self._context_need != "full" and step >= 1 and _has_successful_data_result(raw_result_strings):
    tool_results_content.append({"type": "text", "text": _DATA_SUCCESS_NUDGE})
```

Investigation queries never get nudged to stop early.

### 3. Prompt Changes in `unified_agent.py`

Both conditional on `context_need == "full"`:

**a) Output instructions — replace one-sentence constraint**

The investigation prompt stripping (lines 671-731) already removes `<common_queries>`, `<workspace_rules>`, etc. Additionally replace the output format block for investigation:

Current (all queries):
```
return ONLY ONE sentence summarizing the result.
Do NOT include a markdown table, raw JSON, or SQL.
```

Investigation replacement:
```
Present your findings progressively as you investigate.
After each tool result, share what you learned before continuing.
Build a chronological narrative — explain what happened, when, and why.
When you've found the root cause, present a clear summary.
```

DATA/FINANCIAL queries keep the existing output instructions unchanged.

**Implementation**: Inside the existing `if self._context_need == "full":` block (lines 674-731), add a regex replacement for `<output_instructions>`:
```python
base = _re.sub(
    r"<output_instructions>.*?</output_instructions>",
    INVESTIGATION_OUTPUT_INSTRUCTIONS,
    base, flags=_re.DOTALL,
)
```

**b) Add systemnote expertise block (~150 tokens)**

Appended at the end of the `if self._context_need == "full":` block (before `parts = [base]` at line 734), so it appears near the end of the system prompt where attention is highest (per "Lost in the Middle" research):

```xml
<systemnote_expertise>
To investigate "why" questions, query the systemnote table:
- Filter: recordtypeid = -30 (transactions), recordid = <internal_id>
- BUILTIN.DF(sn.field) does NOT work (static list error) — read raw field names
- Field names use internal notation: TRANDOC.KSTATUS (status), CUSTBODY_* (custom body fields)
- Infer meaning from naming conventions: CUSTBODY_FW_HOLD_EDI_TRANSMIT = EDI hold flag
- context column: SLT=Suitelet, MPR=Map/Reduce, UIF=User Interface, CSV=Import
- name = -4 means system/script action, positive numbers are user IDs
- Order results by date ASC for chronological narrative
</systemnote_expertise>
```

### 4. Orchestrator Changes

**a) Proven patterns already skipped for FULL** — verified at line 1074: `_need_patterns = context_need in (ContextNeed.DATA,)`. No change needed.

**b) Fix classification regex**

Add to `_INVESTIGATION_RE`:
```python
r"history|timeline|trace|audit.?trail|what.?happened|how.?long|when.?was"
```

**False positive risk**: "history" is broad — "show me purchase history for customer X" is DATA, not investigation. Acceptable tradeoff because FULL is a superset of DATA — false positives degrade cost/speed (more steps, full rows) but not correctness. Monitor and tighten patterns if needed.

### 5. What Does NOT Change

| Component | Change? | Why |
|---|---|---|
| Entity resolution | No* | *Vernacular already skipped for FULL by injection matrix. Entity resolution still runs on the task string (field mappings appended). Acceptable — investigation queries typically reference specific records by ID, not entity names. |
| Schema injection | No | Advantage over Claude + MCP |
| SuiteQL pre-validation | No | Catches syntax errors |
| Read-only enforcement | No | Security boundary |
| Rate limiting | No | DOS prevention |
| Financial report formatting | No | Works well |
| Data table rendering (5-row preview for DATA) | No | Frontend renders full table |
| Haiku routing | No | 10x cheaper for simple lookups |
| History condensation | No | Manages long conversations |
| Confidence scoring | No | Useful signal |
| Pivot tool | No | LLM can't do this reliably |
| Audit logging | No | Compliance |
| Proven patterns for DATA | No | Reuses working SQL |
| Tool result interception for DATA | No | Frontend tables work great |

---

## Files Changed

| File | Change | Risk |
|------|--------|------|
| `backend/app/services/chat/agents/base_agent.py` | Early exit guard, nudge guard (2 single-line changes) | Low |
| `backend/app/services/chat/agents/unified_agent.py` | max_steps 10→12, output_instructions replacement, systemnote block | Low — conditional |
| `backend/app/services/chat/orchestrator.py` | Fix `_INVESTIGATION_RE` regex (proven patterns already handled) | Low — regex only |

Total: ~25 lines of code changes across 3 files.

---

## Test Plan

### Unit Tests
- `_classify_context_need()` returns FULL for "give me history", "what happened", "how long was this held"
- `max_steps` returns 12 when `_context_need == "full"`, 6 otherwise
- Early exit does NOT trigger when `_context_need == "full"`
- Data success nudge does NOT append when `_context_need == "full"`
- Proven patterns NOT injected when `context_need == FULL`
- Systemnote expertise block present in prompt when `_context_need == "full"`, absent otherwise
- Output instructions say "progressive" for investigation, "ONLY ONE sentence" for DATA

### Integration Tests (Manual — Staging)
| Scenario | Expected Behavior |
|---|---|
| "Why was order X held?" | Multi-step investigation: find order → systemnote → chronological narrative |
| "Give me order X history" | Classified as FULL, progressive narrative output |
| "When was this sent to 3PL?" | Finds CUSTBODY_FW_SENT_TO_TECHDATA via systemnote, explains timeline |
| "Show me top 10 customers by revenue" | Unchanged — DATA path, one-sentence + data table |
| "Show me P&L for Feb 2026" | Unchanged — FINANCIAL path, report formatting |
| "What does this script do?" | Unchanged — WORKSPACE path |

### Regression Checks
- DATA queries still get 5-row preview, one-sentence output, early exit
- FINANCIAL queries still get report formatting
- Haiku routing still works for simple lookups
- Proven patterns still injected for DATA queries

---

## Success Criteria

1. Investigation queries produce multi-paragraph chronological narratives (not one sentence)
2. Agent follows evidence chain end-to-end without stopping at first result
3. Systemnote queries use `recordtypeid = -30` and read raw field names correctly
4. Output streams progressively between tool calls (not all at once at the end)
5. DATA/FINANCIAL queries are completely unaffected
6. Agent matches or approaches Claude + MCP quality on the benchmark: "Give me order R850152063 history"
