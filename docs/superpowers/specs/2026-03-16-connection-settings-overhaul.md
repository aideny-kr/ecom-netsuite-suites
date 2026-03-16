# Connection Settings Overhaul — Design Spec

## Problem

The Settings page has separate, disconnected sections for OAuth connections and MCP connectors. There's no way to delete stale connections, no token expiry detection, no editable Client IDs or RESTlet URL, and the connection status shows stale DB values without real health checks. Non-admin users see per-connection status dots but admins need full management capabilities.

## Scope

- Grouped "NetSuite Connections" section replacing separate OAuth + MCP sections
- Per-connection delete, re-authenticate, and test actions
- Token expiry detection via health check endpoint (called on page load)
- Editable Client IDs for OAuth and MCP (separate IDs)
- Editable RESTlet URL
- Status colors: green (active), yellow (expired/needs re-auth), gray (pending/inactive), red pulsing (error)

Out of scope: background cron health checks, connection creation wizard redesign, non-NetSuite providers.

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Health check trigger | On Settings page load | Simple, no cron infrastructure needed |
| Client ID storage | Per-connection (encrypted_credentials + metadata) | Already exists, no migration |
| RESTlet URL storage | Connection.metadata_json["restlet_url"] | Already exists, no migration |
| Delete behavior | Soft-delete (status→revoked), hidden from UI | Existing pattern, preserves audit trail |
| Non-admin view | Simplified status dots only | Admin-only for management actions |

## Backend

### New endpoint: `GET /api/v1/connections/health`

Checks all OAuth connections and MCP connectors for the tenant. For each:
- Decrypt credentials, check `expires_at` vs current time
- If token expired → update `status = "needs_reauth"` in DB
- Return per-connection health status

```python
@router.get("/health")
async def check_connection_health(
    user: Annotated[User, Depends(require_permission("connections.view"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
```

Response:
```python
class ConnectionHealthResponse(BaseModel):
    connections: list[ConnectionHealthItem]
    mcp_connectors: list[ConnectionHealthItem]

class ConnectionHealthItem(BaseModel):
    id: str
    label: str
    provider: str
    status: str  # "active", "needs_reauth", "error", "pending", "revoked"
    token_expires_at: str | None  # ISO datetime
    token_expired: bool
    last_health_check: str | None
```

Logic:
- For each connection with `auth_type == "oauth2"`: decrypt, check `expires_at < now()`
- If expired and status is "active": update to "needs_reauth", set `error_reason = "Token expired"`
- For MCP connectors: same token check
- Update `last_health_check_at` on all checked connections
- Return combined list

### New endpoint: `PATCH /api/v1/connections/{id}/client-id`

Updates OAuth Client ID stored in encrypted credentials.

```python
@router.patch("/{connection_id}/client-id")
async def update_client_id(
    connection_id: uuid.UUID,
    request: ClientIdUpdate,  # { client_id: str }
    user: Annotated[User, Depends(require_permission("connections.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
```

- Decrypts credentials, updates `client_id` field, re-encrypts
- Audit logged

### New endpoint: `PATCH /api/v1/mcp-connectors/{id}/client-id`

Same pattern for MCP connectors — updates both `encrypted_credentials["client_id"]` and `metadata_json["client_id"]`.

### New endpoint: `PATCH /api/v1/connections/{id}/restlet-url`

Updates RESTlet URL in `metadata_json`.

```python
@router.patch("/{connection_id}/restlet-url")
async def update_restlet_url(
    connection_id: uuid.UUID,
    request: RestletUrlUpdate,  # { restlet_url: str }
    user: Annotated[User, Depends(require_permission("connections.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
```

- Updates `connection.metadata_json["restlet_url"]`
- Audit logged

### Existing endpoints used (no changes needed)

- `DELETE /api/v1/connections/{id}` — soft-delete (already exists)
- `DELETE /api/v1/mcp-connectors/{id}` — soft-delete (already exists)
- `POST /api/v1/connections/{id}/reconnect` — re-auth flow (already exists)
- `POST /api/v1/connections/{id}/test` — test health (already exists)
- `POST /api/v1/mcp-connectors/{id}/test` — test health (already exists)
- `POST /api/v1/mcp-connectors/{id}/reauthorize` — re-auth (already exists)

### No migrations needed

All data stored in existing columns: `encrypted_credentials`, `metadata_json`, `status`.

## Frontend

### New component: `frontend/src/components/settings/netsuite-connections-section.tsx`

Replaces the separate "NetSuite Connection Section" and "MCP Connectors" section in admin Settings.

Layout:
```
NetSuite Connections
Manage OAuth API and MCP tool connections

┌─ OAuth API Connections ─────────────────────────────────┐
│ Client ID: [abc123...apps.googleus...com] [Edit]        │
│                                                          │
│ ● NetSuite 6738075    oauth2   Connected        [⋮]    │
│ ○ FW NetSuite         oauth2   Pending          [⋮]    │
│ ◉ NetSuite 6738075    oauth2   Token Expired    [⋮]    │
│                                                          │
│ RESTlet URL: [https://6738075.restlets...]       [Edit] │
│                                                          │
│                              [+ Connect NetSuite]       │
├─ MCP Tool Connections ──────────────────────────────────┤
│ Client ID: [xyz789...apps.googleus...com] [Edit]        │
│                                                          │
│ ● Staging   netsuite_mcp   Connected (11 tools)  [⋮]   │
│ ◉ Prod      netsuite_mcp   Re-auth Required      [⋮]   │
│                                                          │
│                              [+ Connect MCP]            │
└──────────────────────────────────────────────────────────┘
```

**Status dot colors:**
- Green (`bg-green-500`) — active
- Yellow (`bg-yellow-500`) — needs_reauth, token expired
- Gray (`bg-muted-foreground/30`) — pending, inactive
- Red pulsing (`bg-red-500 animate-pulse`) — error, revoked

**⋮ kebab menu per connection:**
- Re-authenticate — triggers OAuth popup flow
- Test Connection — calls test endpoint, shows result toast
- Delete — confirm dialog, then soft-delete

**Client ID display:**
- Shows truncated Client ID (first 20 chars + `...`)
- "Edit" button → inline input to update
- If not set, shows "Not configured" with "Set" button

**RESTlet URL display:**
- Shows current URL or "Not configured"
- "Edit" button → inline input to update
- Only shown in OAuth section (MCP doesn't use RESTlet)

**Connect buttons:**
- "+ Connect NetSuite" → existing OAuth authorize flow
- "+ Connect MCP" → existing MCP authorize flow

### New hook: `frontend/src/hooks/use-connection-health.ts`

```typescript
export function useConnectionHealth() {
  return useQuery<ConnectionHealthResponse>({
    queryKey: ["connection-health"],
    queryFn: () => apiClient.get("/api/v1/connections/health"),
    staleTime: 30_000,  // refresh every 30s if page stays open
  });
}
```

### Modified hooks

**`frontend/src/hooks/use-connections.ts`** — add:
```typescript
export function useUpdateClientId()  // PATCH /api/v1/connections/{id}/client-id
export function useUpdateRestletUrl()  // PATCH /api/v1/connections/{id}/restlet-url
```

**`frontend/src/hooks/use-mcp-connectors.ts`** — add:
```typescript
export function useUpdateMcpClientId()  // PATCH /api/v1/mcp-connectors/{id}/client-id
```

### Settings page changes

In `frontend/src/app/(dashboard)/settings/page.tsx`:
- Remove: `<NetSuiteConnectionSection />` (existing)
- Remove: MCP Connectors inline section (existing, ~150 lines of inline JSX)
- Add: `<NetSuiteConnectionsSection />` (new grouped component) in the admin-only block

Non-admin users continue to see the simplified `<ConnectionStatusSection />` with per-connection dots.

## Testing

### Backend tests

| Test | Assertion |
|------|-----------|
| `test_health_check_returns_status` | Returns connection + MCP connector health items |
| `test_health_check_detects_expired_token` | Expired token → status updated to "needs_reauth" |
| `test_health_check_requires_auth` | 401/403 without token |
| `test_update_client_id` | Client ID updated in encrypted credentials |
| `test_update_restlet_url` | RESTlet URL updated in metadata_json |
| `test_update_client_id_requires_permission` | 403 without connections.manage |

### Frontend

- Build passes with new component
- Manual QA: delete connection, re-auth, edit client ID, edit RESTlet URL

## Files Changed

| File | Change |
|------|--------|
| `backend/app/api/v1/connections.py` | Add health, client-id, restlet-url endpoints |
| `backend/app/api/v1/mcp_connectors.py` | Add client-id endpoint |
| `frontend/src/components/settings/netsuite-connections-section.tsx` | **New** — grouped connection management |
| `frontend/src/hooks/use-connection-health.ts` | **New** — health check hook |
| `frontend/src/hooks/use-connections.ts` | Add client-id and restlet-url mutations |
| `frontend/src/hooks/use-mcp-connectors.ts` | Add client-id mutation |
| `frontend/src/app/(dashboard)/settings/page.tsx` | Replace separate sections with grouped component |
| `backend/tests/test_connection_health.py` | **New** — 6 tests |
