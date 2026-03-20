# Consultant Mode — Holistic Investigation & Memory

**Date**: 2026-03-20
**Priority**: HIGH — the gap between "query tool" and "NetSuite consultant"
**Goal**: When the user asks "why?", the agent investigates like a consultant — checks data, reads scripts, understands rules, builds persistent knowledge

---

## Problem

The agent answers "what" questions well (data queries) but fails at "why" questions. When a user asks "why wasn't this order sent to 3PL?", a consultant would:

1. Check the field value (is it true/false?)
2. Search for automation scripts that set this field
3. Read the script logic to understand conditions
4. Explain WHY this specific order didn't meet the criteria
5. **Remember** what they learned for next time

Our agent does step 1 and stops. It has the workspace tools to do steps 2-4 but doesn't think to use them for "why" questions. And it has no mechanism for step 5 — each session starts from scratch.

---

## Vision: The NetSuite Consultant Agent

### What a consultant does that our agent doesn't:

1. **Cross-references data with logic** — "The field is false. Let me check why." → searches scripts → "The User Event on Sales Order checks if item.class = 'Laptop' AND order.total > 500. Your order is class 'Accessory' so it was skipped."

2. **Builds institutional knowledge** — after investigating once, remembers: "3PL routing is controlled by `customscript_fw_3pl_router.js`. It triggers on afterSubmit for Sales Orders. Conditions: item class must be Laptop or Desktop, order total > 0, location must be US warehouse."

3. **Connects the dots across systems** — "This RMA wasn't received because the Item Receipt was created but the status shows Pending Receipt. Let me check the receiving script... the script checks custbody_wmsse_order_type which is empty on this transaction."

4. **Proactively warns** — "You're asking about 3PL routing. Based on what I know about the scripts, orders with class 'Accessory' are never routed. Is that intentional?"

---

## Architecture

### Phase 1: "Why" Detection + Script Investigation

When the user asks a "why" question about a field value:

1. **Detect "why" intent** — classify questions like "why wasn't this sent?", "why is this field empty?", "how does this get set?" as investigation queries
2. **Search workspace for related scripts** — `workspace_search` for the field name (e.g., "custbody_fw_sent_to_techdata" or "3PL")
3. **Read relevant scripts** — `workspace_read_file` on matches
4. **Extract and explain conditions** — parse the script logic and explain in plain English why the specific record did/didn't match

**Prompt addition** (to `<agentic_workflow>`):
```
STEP 4b — "WHY" INVESTIGATION:
If the user asks WHY a field has a certain value (or is empty):
1. First query the data to confirm the current value
2. Search workspace scripts for the field's script_id
3. Read the relevant script to understand the automation logic
4. Explain the conditions and why this specific record matched/didn't match
Do NOT answer "why" questions with just the field value — investigate the logic.
```

### Phase 2: Persistent Field Knowledge (Tenant Soul)

After investigating a field, save what was learned:

```python
# Auto-save after successful "why" investigation
await tenant_save_learned_rule(
    rule_description=(
        "custbody_fw_sent_to_techdata is set by customscript_fw_3pl_router.js "
        "(User Event, afterSubmit on Sales Order). Conditions: "
        "item.class IN ('Laptop', 'Desktop'), order.total > 0, "
        "location IN (US warehouses). If conditions not met, field stays 'F'."
    ),
    rule_category="field_logic",
)
```

Next time someone asks about 3PL, the agent already knows the logic from `<learned_rules>` — no script search needed.

### Phase 3: Script Logic Index

Pre-index all workspace scripts into a structured knowledge base:

| Field | Script | Trigger | Event | Conditions |
|-------|--------|---------|-------|------------|
| custbody_fw_sent_to_techdata | customscript_fw_3pl_router.js | Sales Order | afterSubmit | class IN (Laptop, Desktop), total > 0 |
| custbody_fw_solidus_order_total | customscript_fw_solidus_sync.js | Sales Order | afterSubmit | custbody_fw_channel = 'Solidus' |

This index would be built by a Celery task that:
1. Reads all workspace scripts
2. Extracts field references (custbody_*, custcol_*, etc.)
3. Identifies trigger conditions
4. Stores in a `script_field_index` table
5. Injected into agent context when the field is referenced

---

## Scope for First Implementation

### In scope (Phase 1 only):
- Detect "why" questions about field values
- Auto-search workspace for the field's script_id
- Read and explain the script logic
- Save findings as a learned rule

### Out of scope (future):
- Script logic index (Phase 3 — needs parsing infrastructure)
- Proactive warnings ("this order won't be routed because...")
- Cross-system investigation (checking multiple scripts in sequence)
- Automated script analysis on metadata discovery

---

## Implementation Plan

### Files to modify:

| File | Change |
|------|--------|
| `backend/app/services/chat/agents/unified_agent.py` | Add "WHY" investigation step to workflow |
| `backend/app/services/chat/orchestrator.py` | Detect "why" intent in context classification |

### Files to create:

None — uses existing workspace tools and learned rules.

### Estimated effort:
- Phase 1: ~1 hour (prompt changes + intent detection)
- Phase 2: ~30 min (auto-save learned rule after investigation)
- Phase 3: ~4 hours (script parser + index + Celery task)

---

## Success Criteria

1. User asks "why wasn't R250431178 sent to 3PL?" → agent checks the field, searches scripts, reads the router script, explains the condition that wasn't met
2. Second time someone asks about 3PL routing → agent answers from learned rules without searching scripts
3. Zero false positives on "why" detection — "why is NetSuite slow?" should NOT trigger script investigation
4. Agent still answers "what" questions quickly (no regression on data queries)

---

## Key Insight

The difference between a query tool and a consultant is **investigation depth + institutional memory**. The query tool answers "what is the value?" The consultant answers "why is it that value, what controls it, and what would need to change?"

Our agent has all the tools to be a consultant — workspace search, file reading, learned rules. It just doesn't know WHEN to use them. Phase 1 teaches it when. Phase 2 teaches it to remember. Phase 3 makes it proactive.
