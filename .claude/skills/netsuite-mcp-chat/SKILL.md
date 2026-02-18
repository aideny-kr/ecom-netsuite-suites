---
name: netsuite-mcp-chat
description: >
  NetSuite MCP-based chat system — AI orchestration with MCP tools for querying NetSuite data,
  managing workspaces, and running SuiteQL queries through the chat interface. Use this skill
  whenever the user mentions the chat system, AI assistant, MCP tools, tool calling, system
  prompts, prompt templates, chat orchestration, tool registry, governance, tenant profiles,
  policy profiles, or wants to understand how the AI chat interacts with NetSuite. Also trigger
  when the user wants to add new chat tools, modify the agentic loop, change prompt templates,
  update tool governance settings, or troubleshoot chat/tool-calling issues. If someone asks
  "how does the chat work" or "add a new tool to the chat," this is the skill to use.
---

# NetSuite MCP Chat System

You are an expert in this project's AI chat architecture — the multi-step agentic loop that
connects Claude with NetSuite data through MCP tools. You understand the full stack from
prompt engineering through tool discovery, execution, and governance.

## Architecture Overview

The chat system implements an agentic AI loop:

```
User Message
    ↓
Sanitization (strip prompt injection)
    ↓
RAG Retrieval (relevant docs from DocChunk embeddings)
    ↓
System Prompt Assembly (tenant profile + policy + template)
    ↓
Tool Discovery (local MCP tools + external MCP connectors)
    ↓
Agent Loop (max 5 iterations):
    Claude → Tool Calls → Results → Claude → ...
    ↓
Response + Audit Log
```

### Key Backend Files

| File | Purpose |
|------|---------|
| `backend/app/services/chat/orchestrator.py` | Main agentic loop, message handling |
| `backend/app/services/chat/tools.py` | Tool definition conversion for Anthropic API |
| `backend/app/services/chat/prompts.py` | System prompts and prompt constants |
| `backend/app/services/chat/nodes.py` | Tool execution dispatch, allowed tools list |
| `backend/app/mcp/registry.py` | Tool registry with schemas |
| `backend/app/mcp/governance.py` | Rate limiting, entitlements, timeouts, audit |
| `backend/app/mcp/server.py` | MCP server endpoints |
| `backend/app/services/prompt_template_service.py` | Dynamic prompt generation from tenant profiles |

## Tool System

### Available Chat Tools

The chat currently exposes these tools (defined in `ALLOWED_CHAT_TOOLS`):

| Tool Name | Purpose |
|-----------|---------|
| `netsuite.suiteql` | Execute SuiteQL queries against NetSuite |
| `netsuite.connectivity` | Test NetSuite connection health |
| `data.sample_table_read` | Read sample data from canonical tables |
| `report.export` | Export data as CSV/JSON |
| `workspace.list_files` | List files in a SuiteScript workspace |
| `workspace.read_file` | Read a specific workspace file |
| `workspace.search` | Search across workspace files |
| `workspace.propose_patch` | Propose code changes as a changeset |

Plus any **external MCP tools** from connected MCP servers (like NetSuite MCP).

### Tool Registration Pattern

Each tool is registered in `TOOL_REGISTRY`:

```python
TOOL_REGISTRY = {
    "tool.name": {
        "description": "What this tool does — Claude sees this description",
        "execute": async_function,       # The handler
        "params_schema": {
            "param_name": {
                "type": "string",        # string, number, boolean, array, object
                "required": True,
                "description": "What this parameter does"
            }
        }
    }
}
```

### Adding a New Tool

To add a new tool to the chat system:

1. **Create the tool handler** in `backend/app/mcp/tools/`:
   ```python
   # backend/app/mcp/tools/my_tool.py
   async def execute(params: dict, context: dict | None = None, **kwargs) -> dict:
       tenant_id = context.get("tenant_id")
       db = context.get("db")
       # ... business logic
       return {"result": "data", "status": "ok"}
   ```

2. **Register in the tool registry** (`backend/app/mcp/registry.py`):
   ```python
   from app.mcp.tools import my_tool

   TOOL_REGISTRY["my.tool"] = {
       "description": "Description Claude will see",
       "execute": my_tool.execute,
       "params_schema": {
           "query": {"type": "string", "required": True, "description": "..."}
       }
   }
   ```

3. **Add governance config** (`backend/app/mcp/governance.py`):
   ```python
   TOOL_CONFIGS["my.tool"] = {
       "default_limit": 100,
       "max_limit": 1000,
       "timeout_seconds": 30,
       "rate_limit_per_minute": 30,
       "requires_entitlement": "mcp_tools",
       "allowlisted_params": ["query", "limit"]
   }
   ```

4. **Allow in chat** (`backend/app/services/chat/nodes.py`):
   ```python
   ALLOWED_CHAT_TOOLS = frozenset({
       # ... existing tools
       "my.tool",
   })
   ```

### External MCP Tools

External tools come from connected MCP servers (NetSuite MCP, etc.). They're:
- Discovered via `discover_tools()` which connects and calls `list_tools()`
- Named with prefix: `ext__{connector_id_hex}__{tool_name}` (max 64 chars)
- Executed via `call_external_mcp_tool()` with OAuth2 token refresh
- Subject to the same governance layer as local tools

## Prompt System

### Prompt Template Architecture

The system uses a layered prompt approach:

1. **Static base prompts** (`prompts.py`) — hardcoded constants
2. **Dynamic tenant templates** (`prompt_template_service.py`) — generated from tenant profile
3. **RAG context** — relevant docs injected per-query

### Tenant Profile Fields

The dynamic template is built from:

```python
TenantProfile:
    industry: str              # e.g., "Retail", "Manufacturing"
    business_description: str  # What the company does
    team_size: str
    netsuite_account_id: str
    subsidiaries: list[str]
    chart_of_accounts: list[dict]  # account names/numbers
    item_types: list[str]
    custom_segments: list[dict]
    suiteql_naming: dict       # Custom field naming conventions
    fiscal_calendar: dict      # Fiscal year settings
```

### Policy Profile Fields

Constrains what the AI can do:

```python
PolicyProfile:
    read_only_mode: bool           # Block all write operations
    allowed_record_types: list     # Only these records accessible
    blocked_fields: list           # Never expose these fields
    tool_allowlist: list           # Only these tools available
    max_rows_per_query: int        # Cap on SuiteQL results
    require_row_limit: bool        # Force ROWNUM in queries
    custom_rules: list[str]        # Free-form constraints
```

### Template Sections

The generated system prompt contains these sections:

| Section | Content |
|---------|---------|
| Identity | Industry, business description, team context |
| NetSuite Context | Account ID, subsidiaries, CoA, item types, custom segments |
| SuiteQL Rules | ROWNUM syntax, NVL(), no CTEs, naming conventions |
| Tool Rules | When to use each tool, multi-step workflows, error recovery |
| Policy Constraints | Read-only mode, blocked fields, row limits |
| Response Rules | Formatting, no fabrication, error handling |

### Generating/Updating Templates

```python
# Generate and save a new template
await generate_and_save_template(db, tenant_id, tenant_profile)

# Retrieve active template for chat
template_text = await get_active_template(db, tenant_id)
```

Templates are versioned — creating a new one deactivates the previous one.

## Agentic Loop Details

### How the Loop Works

The orchestrator runs up to `CHAT_MAX_TOOL_CALLS_PER_TURN` (default 5) iterations:

1. Build messages array (system prompt + conversation history + RAG context)
2. Call Claude with tools definitions
3. If Claude responds with `tool_use` blocks:
   - Execute each tool call via `execute_tool_call()`
   - Append tool results to messages
   - Loop back to step 2
4. If Claude responds with text only: return to user
5. Log everything with `correlation_id` for audit

### Error Handling in Tool Calls

When a tool call fails:
- The error is returned as a tool result (not thrown)
- Claude sees the error and can retry or adjust
- The governance layer enforces timeouts and rate limits independently
- Audit logs capture both successes and failures

### Conversation History

- Stored in `ChatSession` + `ChatMessage` models
- Trimmed to `CHAT_MAX_HISTORY_TURNS` (default 20) most recent messages
- Session types: `chat` (normal) and `onboarding` (guided setup)

## Governance Layer

Every tool call passes through governance:

```python
async def governed_execute(
    tool_name, params, tenant_id, actor_id,
    execute_fn, correlation_id, db
) -> dict
```

Checks applied:
1. **Rate limiting** — per tenant, per minute, per tool
2. **Entitlement verification** — does the tenant have access to this tool?
3. **Timeout enforcement** — kill long-running tool calls
4. **Parameter allowlisting** — only approved params forwarded
5. **Audit logging** — every call logged with timing and results

### Rate Limit Configuration

Default: `MCP_RATE_LIMIT_PER_MINUTE = 60` (from `config.py`)

Per-tool overrides in `TOOL_CONFIGS`:
```python
"netsuite.suiteql": {
    "rate_limit_per_minute": 30,  # More conservative for DB queries
    "timeout_seconds": 30,
}
```

## Troubleshooting Chat Issues

### Tool not appearing in chat
1. Is it registered in `TOOL_REGISTRY`?
2. Is it in `ALLOWED_CHAT_TOOLS`?
3. Does the tenant have the required entitlement?
4. Is there a policy `tool_allowlist` that excludes it?

### Tool call failing
1. Check governance — rate limited? Timed out?
2. Check the tool's error response format
3. Check the audit logs for the `correlation_id`
4. For external MCP tools: is the connector active? Are OAuth tokens valid?

### Claude not using tools effectively
1. Review the system prompt — does it guide tool usage well?
2. Check the tool descriptions — are they clear about when to use each tool?
3. Review the tenant profile — is NetSuite context populated?
4. Check the policy — is read_only_mode blocking needed tools?

### SuiteQL queries failing
1. Check the table allowlist in `NETSUITE_SUITEQL_ALLOWED_TABLES`
2. Is the query using NetSuite SQL syntax? (ROWNUM, NVL, no CTEs)
3. Is `enforce_limit()` injecting the row cap correctly?
4. Is the OAuth connection valid? Check token refresh logs.
