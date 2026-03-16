# Connection Settings Overhaul Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Grouped NetSuite connection management with health checks, editable Client IDs, RESTlet URL, and delete/re-auth/test actions.

**Architecture:** Add health check endpoint that detects expired tokens. Add client-id and restlet-url update endpoints. Replace separate OAuth/MCP settings sections with one grouped component. No migrations needed.

**Tech Stack:** FastAPI, SQLAlchemy, React, shadcn/ui, lucide-react

**Spec:** `docs/superpowers/specs/2026-03-16-connection-settings-overhaul.md`

---

## Chunk 1: Backend — Health check + update endpoints

### Task 1: Health check + client-id + restlet-url endpoints with tests

**Files:**
- Modify: `backend/app/api/v1/connections.py`
- Modify: `backend/app/api/v1/mcp_connectors.py`
- Create: `backend/tests/test_connection_health.py`

**New endpoints on connections.py:**

1. `GET /api/v1/connections/health` — checks all OAuth + MCP connections, detects expired tokens, updates status
2. `PATCH /api/v1/connections/{id}/client-id` — updates client_id in encrypted credentials
3. `PATCH /api/v1/connections/{id}/restlet-url` — updates restlet_url in metadata_json

**New endpoint on mcp_connectors.py:**

4. `PATCH /api/v1/mcp-connectors/{id}/client-id` — updates client_id in encrypted credentials + metadata_json

All use `Annotated[Type, Depends()]`, audit logged, `await db.commit()`.

Health check logic:
- For each OAuth connection with auth_type=="oauth2": decrypt credentials, check if `expires_at < time.time()`
- If expired and status is "active": set status to "needs_reauth", set error_reason
- Same for MCP connectors
- Update `last_health_check_at` on all checked connections
- Return combined health items

Tests: health returns items, detects expired token, requires auth, client-id update works, restlet-url update works, permission check.

---

## Chunk 2: Frontend — Hooks + grouped component

### Task 2: New hooks + update existing hooks

**Files:**
- Create: `frontend/src/hooks/use-connection-health.ts`
- Modify: `frontend/src/hooks/use-connections.ts`
- Modify: `frontend/src/hooks/use-mcp-connectors.ts`

New hook: `useConnectionHealth()` — calls `GET /api/v1/connections/health`
New mutations: `useUpdateClientId()`, `useUpdateRestletUrl()`, `useUpdateMcpClientId()`

### Task 3: Grouped NetSuite connections component

**Files:**
- Create: `frontend/src/components/settings/netsuite-connections-section.tsx`
- Modify: `frontend/src/app/(dashboard)/settings/page.tsx`

Component shows:
- OAuth section: Client ID (editable), connection rows with status/actions, RESTlet URL (editable), Connect button
- MCP section: Client ID (editable), connection rows with status/tools count/actions, Connect button
- Each row: status dot, label, type, status text, kebab menu (Re-auth, Test, Delete)
- Calls health check on mount to refresh statuses

Settings page: replace `<NetSuiteConnectionSection />` + inline MCP section with `<NetSuiteConnectionsSection />`

### Task 4: Docker rebuild + test + push

- Rebuild backend + frontend
- Run tests
- Push branch
