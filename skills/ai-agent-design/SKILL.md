---
name: ai-agent-design
description: >
  Patterns for building multi-agent AI systems with anti-hallucination guards, semantic routing,
  prompt engineering, entity resolution, confidence scoring, and streaming. Use this skill when
  designing or modifying AI agents, chat orchestration, coordinator routing logic, specialist
  agent behavior, prompt templates, tool calling loops, LLM adapter patterns, or anti-hallucination
  systems. Also trigger when working on SSE streaming, history compaction, confidence extraction,
  judge systems, or multi-provider LLM support. If the user mentions agents, prompts, hallucination,
  routing, or chat architecture, use this skill.
---

# AI Agent Architecture & Anti-Hallucination

This skill documents the multi-agent system architecture used in Suite Studio AI — a production
system that coordinates specialist AI agents to query NetSuite ERP data, search documentation,
analyze results, and manage SuiteScript workspaces. The patterns here are battle-tested against
real enterprise data where hallucination has direct business consequences.

## Architecture Overview

The system follows a **supervisor pattern with semantic routing**:

```
User Message
    ↓
Orchestrator (SSE endpoint)
    ↓
Coordinator (semantic router)
    ├── Heuristic classifier (regex, ~90% of queries)
    └── LLM fallback (ambiguous queries only)
    ↓
Specialist Agent(s)
    ├── SuiteQL Agent (data queries, max_steps=6)
    ├── RAG Agent (documentation, max_steps=3)
    ├── Workspace Agent (code ops, max_steps=5)
    └── Data Analysis Agent (reasoning, max_steps=1)
    ↓
Response (streamed via SSE with <thinking> tags)
```

The key insight: avoid calling the LLM for routing when you don't have to. A fast heuristic
classifier handles ~90% of queries with regex patterns, and only truly ambiguous queries
fall through to an LLM planner. This saves latency and cost on every request.

## Semantic Routing

### Intent Classification

Define clear intent types as an enum. Each maps to a route configuration:

```python
class IntentType(str, enum.Enum):
    DOCUMENTATION = "documentation"
    DATA_QUERY = "data_query"
    FINANCIAL_REPORT = "financial_report"
    WORKSPACE_DEV = "workspace_dev"
    ANALYSIS = "analysis"
    CODE_UNDERSTANDING = "code_understanding"
    AMBIGUOUS = "ambiguous"
```

### Heuristic-First Classification

Check regex patterns in priority order. The order matters because some queries match
multiple categories — you want the most specific match to win:

1. CODE_UNDERSTANDING — script/code terminology ("write script", "refactor", "how does the script")
2. WORKSPACE_DEV — workspace operations ("jest test", "sdf deploy", "file cabinet")
3. DOCUMENTATION — how-to questions ("how do I", "explain syntax", "error code")
4. ANALYSIS — comparative queries ("compare", "trend", "month-over-month", "breakdown")
5. FINANCIAL_REPORT — accounting ("income statement", "P&L", "balance sheet", "GL summary")
6. DATA_QUERY — data retrieval ("show me", "find", "pull", "how many", "sales order")

Only when nothing matches → `AMBIGUOUS` → LLM planner decides.

### Route Registry

Map intents to agent configurations. This makes adding new agents trivial:

```python
ROUTE_REGISTRY: dict[IntentType, RouteConfig] = {
    IntentType.DOCUMENTATION: RouteConfig(agents=["rag"]),
    IntentType.DATA_QUERY: RouteConfig(agents=["suiteql"]),
    IntentType.ANALYSIS: RouteConfig(agents=["suiteql", "analysis"], parallel=False),
    IntentType.FINANCIAL_REPORT: RouteConfig(agents=["suiteql", "analysis"], parallel=False),
    IntentType.WORKSPACE_DEV: RouteConfig(agents=["workspace"]),
}
```

Sequential vs parallel: data analysis must wait for SuiteQL results, so `parallel=False`.
Documentation and workspace can run independently.

## Specialist Agent Design

### Base Agent Pattern

Every specialist extends a common base with a configurable agentic loop:

```python
class BaseSpecialistAgent:
    max_steps: int          # Budget for tool calls
    tools: list[ToolDef]    # Available tools for this agent
    system_prompt: str       # Specialist instructions

    async def run(self, task, context, db, adapter, model) -> AgentResult:
        messages = self._build_initial_messages(task, context)
        for step in range(self.max_steps):
            response = await adapter.create_message(model, messages, tools=self.tools)
            if response.has_tool_calls:
                results = await self._execute_tools(response.tool_calls)
                messages.append(assistant_msg(response))
                messages.append(tool_results_msg(results))
            else:
                return AgentResult(text=response.text, ...)
        # Max steps exhausted — force final response without tools
        messages.append({"role": "user", "content": "You have used all tool steps. Provide your final answer now."})
        response = await adapter.create_message(model, messages, tools=None)
        return AgentResult(text=response.text, ...)
```

**Critical: the forced-final-response pattern.** When the loop exhausts `max_steps`, don't just
return whatever partial state you have. Send one more message explicitly telling the LLM to
synthesize a final answer with NO tools available. This prevents hanging or returning raw tool output.

### Agent Step Budgets

Be intentional about step budgets. More steps = more cost and latency, but too few and
the agent can't recover from mistakes:

| Agent | Steps | Reasoning |
|-------|-------|-----------|
| SuiteQL | 6 | May need: schema lookup → first query → fix error → retry → refine → final |
| RAG | 3 | rag_search → refined search → web_search fallback. Strict budget prevents rabbit holes |
| Workspace | 5 | list files → read file → search → read another → propose patch |
| Analysis | 1 | Pure reasoning on data already retrieved. No tools needed |

### Tool Budget Enforcement

For agents like RAG that tend to over-search, enforce explicit tool budgets:
"You may use at most 2 rag_search calls and 1 web_search call."
State this in the system prompt, not just in code. The LLM respects stated budgets.

## Anti-Hallucination System

This is a multi-layered defense. No single technique catches everything — they work together.

### Layer 1: Force Tool Execution

The most dangerous hallucination is when the LLM answers a data question from conversation
memory instead of querying the source of truth. Guard against this at step 0:

```python
def _task_contains_query(task: str) -> bool:
    """Detect if task requires tool execution."""
    if re.search(r'SELECT\s+', task, re.IGNORECASE):
        return True
    keywords = ["how many", "total", "count", "sum", "average",
                "quantity", "revenue", "sales", "orders", "inventory"]
    return any(kw in task.lower() for kw in keywords)
```

If step 0 returns text without any tool calls AND the task contains a data question,
inject a message: "You MUST execute the query using the tool — do NOT answer from memory."
Then continue the loop.

### Layer 2: SuiteQL Judge (Post-Execution Verification)

After a query executes successfully, a lightweight judge (Haiku, 3-second timeout) verifies
the result makes sense:

```python
async def judge_suiteql_result(user_question, sql, result_preview, row_count) -> JudgeVerdict:
    """
    Checks:
    1. Does the query address the user's question?
    2. Are the results sensible (not all nulls, not suspiciously uniform)?
    3. Are the selected columns relevant?
    4. If asking for aggregation, does query use GROUP BY?
    Returns: JudgeVerdict(approved: bool, confidence: float, reason: str)
    """
```

**Critical design choice: fail-open.** If the judge times out or errors, return `approved=True`.
A false-negative (blocking a good query) is worse than a false-positive (letting a questionable
one through with a warning). The judge adds safety, not gates.

### Layer 3: Tier-Based Confidence Thresholds

Not all queries are equally important. A casual "show me some recent orders" doesn't need
the same verification rigor as "what's our total revenue this quarter?"

```python
class ImportanceTier(str, Enum):
    CASUAL = "casual"       # Browsing, exploration → low threshold
    OPERATIONAL = "operational"  # Day-to-day decisions → medium threshold
    FINANCIAL = "financial"  # Money, compliance → high threshold
    CRITICAL = "critical"    # Audit, legal → highest threshold
```

`enforce_judge_threshold(verdict, tier) -> EnforcementResult` applies tier-appropriate
confidence requirements. Results that pass with low confidence get a disclaimer appended.

### Layer 4: Composite Confidence Scoring

Don't rely on a single confidence signal. Combine multiple orthogonal signals:

```python
composite = CompositeScorer(
    llm_score=llm_confidence / 5.0,           # Self-assessed (often overconfident)
    query_pattern_similarity=0.85,              # How similar to known-good patterns
    domain_knowledge_similarity=0.72,           # RAG relevance score
    entity_resolution_confidence=0.91,          # Were entity names resolved cleanly?
    tool_success_rate=successful / total,        # Did tools execute without errors?
    num_tool_calls=total,
    required_tool_calls=required,
).compute()
```

When composite confidence is low (≤2/5), append a disclaimer:
"*Note: I'm not fully confident in this result. Please verify the data before acting on it.*"

### Layer 5: Golden Query Regression Suite

Maintain a curated set of queries with known-correct SQL and expected result shapes.
Run these as regression tests to catch prompt drift:

```json
{
  "id": "revenue-by-month",
  "tier": "financial",
  "natural_language": "Show me monthly revenue for 2025",
  "expected_sql_patterns": ["SUM(", "GROUP BY", "trandate", "CustInvc"],
  "anti_patterns": ["SalesOrd.*CustInvc", "foreigntotal.*transactionline"]
}
```

## Entity Resolution

Translating human language ("the Platform field", "Inventory Processor record") into
NetSuite internal IDs (`custitem_fw_platform`, `customrecord_r_inv_processor`) is critical.

### Two-Stage Pipeline

**Stage 1: Fast NER (Haiku, ~200ms)**
Extract named entities from the user message. Only extract tenant-specific terms —
generic NetSuite types like "sales order" don't need resolution.

**Stage 2: pg_trgm Fuzzy Matching**
Match extracted entities against `tenant_entity_mapping` table using trigram similarity:
```sql
SELECT * FROM tenant_entity_mapping
WHERE tenant_id = :tid AND natural_name % :entity
ORDER BY similarity(natural_name, :entity) DESC
LIMIT 1
```

### Vernacular Injection

Resolved entities are injected into the agent prompt as XML:
```xml
<tenant_vernacular>
  <resolved_entities>
    <entity>
      <user_term>Platform</user_term>
      <internal_script_id>custitem_fw_platform</internal_script_id>
      <entity_type>custom_field</entity_type>
      <confidence_score>0.92</confidence_score>
    </entity>
  </resolved_entities>
</tenant_vernacular>
```

The agent prompt instructs the LLM to use these exact IDs in queries. This eliminates
guessing at custom field names — the #1 source of "Unknown identifier" errors.

## Prompt Engineering Patterns

### Schema Injection

Pre-compile tenant metadata (table schemas, custom fields, custom lists) and inject into
the system prompt via a placeholder: `{{INJECT_CELERY_YAML_METADATA_HERE}}`. This gives
the SuiteQL agent full awareness of available fields without needing a metadata lookup tool call.

### Domain Knowledge Retrieval

For financial queries, retrieve relevant domain knowledge chunks via vector similarity
and inject them into context. Bump retrieval `top_k` to 5 for financial queries (vs 3 for general).
This provides the agent with GL framing, account type conventions, and sign rules.

### Prompt Caching (Cost Optimization)

Mark system prompt and tool definitions with `cache_control: {"type": "ephemeral"}`.
This gets ~90% cost reduction on subsequent calls in the same session — especially valuable
for multi-turn conversations where the system prompt is identical across turns.

## Streaming Architecture

### SSE Event Types

The orchestrator streams events to the frontend:
```python
("text", chunk)           # Token from LLM stream
("tool_status", message)  # "Executing netsuite_suiteql..."
("response", AgentResult) # Final result with metadata
```

### Thinking Tags

Agent reasoning is wrapped in `<thinking>` tags and collapsed in the UI by default.
This gives transparency for debugging without cluttering the user experience.

## History Compaction

Long conversations blow up context windows. Compact aggressively:

- **Threshold:** > 12 messages (6 turns)
- **Strategy:** Keep last 8 messages verbatim, compact older turns into LLM-generated summary
- **Summary preserves:** Current goal, key data points (numbers, dates, IDs), failed strategies, user corrections
- **Summary drops:** Pleasantries, raw data dumps, tool JSON blobs
- **Fail-safe:** Returns original history unchanged on any error

Result: `[compacted_summary, acknowledgment, ...recent_8_messages]`

## Multi-Provider LLM Support

### Adapter Pattern

Abstract base class with concrete implementations per provider:
```python
class BaseLLMAdapter:
    async def create_message(model, max_tokens, system, messages, tools) -> Response
    async def stream_message(model, max_tokens, system, messages, tools) -> AsyncIterator
    def build_tool_result_message(tool_results) -> dict
    def build_assistant_message(response) -> dict
```

Each adapter handles format conversion (Anthropic tool format ↔ OpenAI function calling ↔ Gemini).
This means the agent code never deals with provider-specific formats — it speaks one language
and the adapter translates.

### Provider Selection

Route based on `ai_provider` in tenant config. This enables BYOK (bring your own key)
where each tenant can use their preferred LLM provider.

## Tool Result Handling

### Truncation

Large query results can blow up context. Truncate intelligently:
- Error messages: max 1,000 chars
- Large result sets: max 500 rows
- Include guidance in truncation message: "Use GROUP BY with aggregate functions to get summaries"

### Policy-Based Tool Gating

Check tenant policies before tool execution:
```python
policy_result = policy_evaluate(active_policy, tool_name, tool_input)
if not policy_result["allowed"]:
    result = {"error": f"Policy blocked: {policy_result['reason']}"}
```

Also redact blocked fields from results after execution.

## Error Handling & Retries

### Coordinator Retry Logic

When an agent fails or returns low-confidence results, the coordinator can retry
with modified instructions. Track `budget_remaining` across all agents to prevent
infinite retry loops.

### Step Loop Exhaustion

When max_steps is exceeded, force a final response with `tools=None`. The LLM must
synthesize whatever it has. Never return partial tool output as the final response.
