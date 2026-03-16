# User Control Panel & RBAC — Design Spec

## Problem

The app supports multi-user tenants but has no way for admins to invite team members, no Google Sign-In, and no user management UI. Admins currently create users via API with passwords — there's no invite flow, no self-signup, and no way to manage roles from the frontend. The RBAC foundation exists (4 roles, 12 permissions, UserRole join table) but isn't exposed to users.

## Scope

- **Phase 1**: User management UI in Settings, invite flow with email, role assignment (Admin / User / Operations Only)
- **Phase 2**: Google Sign-In as an auth option during invite acceptance
- **Phase 3**: Seat limits enforcement (free ≤ 20, pro ≤ 50)

Out of scope: SSO/SAML, team-visible voting, per-permission granularity (fixed roles only).

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Role model | 3 fixed roles: Admin, User, Operations Only | Simple, covers the access tiers needed |
| Invite flow | Admin sends invite → user accepts via link | No self-signup, keeps tenant boundary clean |
| Google auth | Available at invite acceptance (not standalone) | User must be invited first, then can choose Google or password |
| UI location | New "Team" tab in Settings page | Consistent with existing settings pattern |
| Vote visibility | Training only (proven patterns / autoresearch) | Votes feed optimization loop, not social signals |

## Existing Foundation

The codebase already has significant RBAC infrastructure. Here's what exists vs what's new:

**Already implemented:**
- `Role`, `Permission`, `RolePermission`, `UserRole` models in `backend/app/models/user.py`
- 4 seeded roles: `admin`, `finance`, `ops`, `readonly` with 12 permissions
- `require_permission(codename)` dependency in `backend/app/core/dependencies.py`
- `has_permission()` async helper
- User CRUD endpoints: `GET /users`, `POST /users`, `PATCH /users/{id}/roles`, `DELETE /users/{id}` in `backend/app/api/v1/users.py`
- Audit logging for user mutations
- Multi-tenant isolation via `user_roles.tenant_id`

**Needs modification:**
- Role names: map `admin` → Admin, `finance` → User (has finance access), `ops` → Operations Only, retire `readonly`
- Frontend `Role` type: currently hardcoded `"owner" | "admin" | "member" | "viewer"` — doesn't match backend
- Entitlement service: no `max_users` limit
- Settings page: no Team/Users tab

**New:**
- Invite model + endpoints + email sending
- Google OAuth (user auth, not NetSuite)
- Frontend Team management UI
- Seat limit enforcement

---

## Backend

### Role Mapping

Align the three user-facing roles to existing backend roles. This avoids a migration — we reuse the seeded role rows.

| User-Facing Name | Backend `roles.name` | Key Permissions | Chat Capabilities |
|-----------------|---------------------|-----------------|-------------------|
| **Admin** | `admin` | All 12 permissions | Full access + soul.md edit + remember + change reporting tier |
| **User** | `finance` | `connections.view`, `tables.view`, `audit.view`, `exports.csv`, `exports.excel`, `recon.run`, `tools.suiteql` | Full chat + finance reports + vote up/down |
| **Operations Only** | `ops` | `connections.manage`, `connections.view`, `tables.view`, `audit.view`, `exports.csv`, `schedules.manage` | Chat access but NO finance reports + vote up/down |

The `readonly` role stays in the DB but isn't exposed in the UI (reserved for future use or API-only integrations).

**Chat permission gating for Operations Only:**
- Add a new permission: `chat.financial_reports` — assigned to `admin` and `finance` roles, NOT `ops`
- In orchestrator.py, before financial report mode activation, check `has_permission(user, "chat.financial_reports")`
- If denied: agent responds with "Financial reports are restricted to your role. Contact your admin for access." — no tool call attempted
- Non-financial chat (SuiteQL queries, RAG, workspace) works normally for ops users

### New permission

**Migration:** `043_chat_financial_permission.py`

```python
def upgrade() -> None:
    # Insert new permission
    op.execute("""
        INSERT INTO permissions (id, codename)
        VALUES (gen_random_uuid(), 'chat.financial_reports')
    """)
    # Assign to admin and finance roles (not ops, not readonly)
    op.execute("""
        INSERT INTO role_permissions (role_id, permission_id)
        SELECT r.id, p.id
        FROM roles r, permissions p
        WHERE r.name IN ('admin', 'finance')
        AND p.codename = 'chat.financial_reports'
    """)

def downgrade() -> None:
    op.execute("""
        DELETE FROM role_permissions
        WHERE permission_id = (SELECT id FROM permissions WHERE codename = 'chat.financial_reports')
    """)
    op.execute("DELETE FROM permissions WHERE codename = 'chat.financial_reports'")
```

### New model: `Invite`

**File:** `backend/app/models/invite.py`

```python
class Invite(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "invites"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    role_name: Mapped[str] = mapped_column(String(50), nullable=False, default="finance")
    invited_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # status: "pending" | "accepted" | "expired" | "revoked"
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "email", "status", name="uq_invite_tenant_email_pending"),
    )
```

**Migration:** `044_invites_table.py`

### New model addition: `max_users` entitlement

**File:** `backend/app/services/entitlement_service.py`

Add `max_users` to plan definitions:

```python
PLAN_FEATURES = {
    "free": {
        # ... existing
        "max_users": 20,
    },
    "pro": {
        # ... existing
        "max_users": 50,
    },
    "max": {
        # ... existing
        "max_users": 999,  # effectively unlimited
    },
}
```

### New service: `backend/app/services/invite_service.py`

```python
async def create_invite(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    email: str,
    role_name: str,
    invited_by: uuid.UUID,
) -> Invite:
    """Create an invite and send email.

    Validations:
    - email not already an active user in this tenant
    - no pending invite for same email in this tenant
    - seat limit not exceeded (count active users + pending invites vs max_users)
    - role_name is valid ("admin", "finance", "ops")

    Token: 32-byte secrets.token_urlsafe() → 43 chars
    Expiry: 7 days from creation
    """

async def accept_invite(
    db: AsyncSession,
    token: str,
    full_name: str,
    password: str | None = None,
    google_id_token: str | None = None,
) -> tuple[User, dict]:
    """Accept an invite — creates user account, assigns role, returns JWT tokens.

    Either password or google_id_token must be provided (not both, not neither).
    If google_id_token: verify with Google, extract email, match against invite email.
    Marks invite as accepted. Creates user + user_role rows.
    Returns (user, tokens_dict).
    """

async def revoke_invite(db: AsyncSession, invite_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
    """Revoke a pending invite. Sets status to 'revoked'."""

async def list_invites(db: AsyncSession, tenant_id: uuid.UUID) -> list[Invite]:
    """List all invites for tenant (pending + accepted, not expired/revoked)."""

async def cleanup_expired_invites(db: AsyncSession) -> int:
    """Mark expired invites. Can run as periodic task."""
```

### Invite email

Use a lightweight email approach. Two options depending on infrastructure:

**Option A — Transactional email service (recommended for production):**
- Use Resend, SendGrid, or AWS SES
- Template: simple HTML with tenant brand_name, inviter name, accept link
- Accept URL: `{FRONTEND_URL}/invite/{token}`
- New env var: `EMAIL_PROVIDER`, `EMAIL_API_KEY`, `EMAIL_FROM_ADDRESS`

**Option B — SMTP fallback (for development):**
- Python `smtplib` + `email.mime`
- Works with any SMTP server (Gmail, Mailtrap for dev)

Email content:
```
Subject: {inviter_name} invited you to {tenant_brand_name} on SuiteStudio

{inviter_name} has invited you to join {tenant_brand_name} on SuiteStudio as a {role_display_name}.

[Accept Invitation] → {FRONTEND_URL}/invite/{token}

This invitation expires in 7 days.
```

### New endpoints: `backend/app/api/v1/invites.py`

```python
router = APIRouter(prefix="/invites", tags=["invites"])

@router.post("", response_model=InviteResponse, status_code=status.HTTP_201_CREATED)
async def create_invite(
    request: InviteCreate,
    user: Annotated[User, Depends(require_permission("users.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Send an invite. Requires users.manage permission (Admin only)."""

@router.get("", response_model=list[InviteResponse])
async def list_invites(
    user: Annotated[User, Depends(require_permission("users.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """List all invites for current tenant."""

@router.delete("/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invite(
    invite_id: str,
    user: Annotated[User, Depends(require_permission("users.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Revoke a pending invite."""

@router.get("/accept/{token}", response_model=InviteAcceptInfo)
async def get_invite_info(token: str):
    """Public endpoint — returns invite details for the accept page (tenant name, email, role, expired?)."""

@router.post("/accept/{token}", response_model=AuthResponse)
async def accept_invite(
    token: str,
    request: InviteAcceptRequest,
):
    """Public endpoint — accepts invite, creates account, returns JWT tokens.

    InviteAcceptRequest:
      full_name: str
      password: str | None       # For email+password signup
      google_id_token: str | None  # For Google Sign-In
    """
```

### Google OAuth verification

**File:** `backend/app/services/google_auth_service.py`

```python
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token_lib

async def verify_google_token(token: str) -> dict:
    """Verify Google ID token and return user info.

    Returns: {"email": str, "name": str, "picture": str | None, "sub": str}
    Raises: ValueError if token is invalid or expired.

    Uses Google's tokeninfo endpoint for verification.
    Client ID validated against GOOGLE_CLIENT_ID env var.
    """
```

**New dependency:** `google-auth` package in `backend/pyproject.toml`

**New env vars:**
- `GOOGLE_CLIENT_ID` — from Google Cloud Console (OAuth 2.0 Client ID, Web application type)
- `GOOGLE_CLIENT_SECRET` — not needed for ID token verification, but needed if we add Google login for existing users later

### Google Sign-In for existing users (login path)

Beyond invite acceptance, existing users who signed up via Google should be able to log back in with Google:

**File:** `backend/app/api/v1/auth.py` — New endpoint:

```python
@router.post("/google", response_model=AuthResponse)
async def google_login(
    request: GoogleLoginRequest,  # { google_id_token: str }
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Login with Google. Only works for users who originally signed up via Google.

    Verifies Google ID token → extracts email → finds user → returns JWT tokens.
    If no user found with this email: 404 (must be invited first).
    """
```

**User model addition:** Add `auth_provider` field to track how user signed up:

```python
auth_provider: Mapped[str] = mapped_column(String(20), nullable=False, default="email")
# Values: "email" | "google"
```

And `google_sub` for Google's unique user ID:

```python
google_sub: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
```

**Migration:** `045_user_google_auth.py`

### Admin-only capabilities enforcement

The following chat capabilities are admin-only. Gate them with permission checks:

| Capability | Permission | Where to enforce |
|-----------|-----------|-----------------|
| Edit soul.md | `tenant.manage` | `backend/app/api/v1/settings.py` — soul endpoint |
| "Remember" (store proven patterns) | `tenant.manage` | Orchestrator — check before storing to `proven_patterns` |
| Change reporting tier | `tenant.manage` | `backend/app/api/v1/settings.py` — chat settings endpoint |
| Invite users | `users.manage` | `backend/app/api/v1/invites.py` |
| Manage connections | `connections.manage` | Already enforced |

For vote up/down on chat results — all roles can vote. Votes feed into the proven patterns / autoresearch training loop but aren't visible to other users. No new permission needed.

### Modify existing user endpoints

**File:** `backend/app/api/v1/users.py`

The existing endpoints are close but need tweaks:

1. `POST /users` — Keep but make it admin-only fallback (invite flow is primary). Add seat limit check.
2. `PATCH /users/{id}/roles` — Restrict: admin cannot demote themselves if they're the last admin.
3. `DELETE /users/{id}` — Restrict: cannot deactivate the last admin. Add "transfer ownership" guard.
4. New: `PATCH /users/{id}` — Update user profile (full_name, email). Users can edit themselves, admins can edit anyone.

### Router registration

**File:** `backend/app/api/v1/router.py`

```python
from app.api.v1.invites import router as invites_router
api_router.include_router(invites_router)
```

---

## Frontend

### Fix Role type mismatch

**File:** `frontend/src/lib/types.ts`

```typescript
// OLD:
export type Role = "owner" | "admin" | "member" | "viewer";

// NEW:
export type RoleName = "admin" | "finance" | "ops" | "readonly";

export interface UserRole {
  role_name: RoleName;
  assigned_at: string;
}

export interface User {
  id: string;
  tenant_id: string;
  tenant_name: string;
  email: string;
  full_name: string;
  roles: RoleName[];      // Backend returns array of role names
  is_active: boolean;
  auth_provider: "email" | "google";
  onboarding_completed_at: string | null;
  created_at: string;
  updated_at: string;
}
```

**Display name mapping** (frontend only — backend stays as-is):

```typescript
export const ROLE_DISPLAY_NAMES: Record<RoleName, string> = {
  admin: "Admin",
  finance: "User",
  ops: "Operations Only",
  readonly: "Read Only",
};

export const ROLE_DESCRIPTIONS: Record<RoleName, string> = {
  admin: "Full access including soul.md, settings, user management, and finance",
  finance: "Chat, analytics, finance reports, and data export",
  ops: "Chat, connections, schedules — no finance reports",
  readonly: "View-only access (reserved)",
};
```

### Permission helper hook

**File:** `frontend/src/hooks/use-permissions.ts`

```typescript
"use client";
import { useAuth } from "@/providers/auth-provider";

// Backend role → permission mapping (mirrors seed data)
const ROLE_PERMISSIONS: Record<RoleName, string[]> = {
  admin: ["tenant.manage", "users.manage", "connections.manage", "connections.view",
          "tables.view", "audit.view", "exports.csv", "exports.excel",
          "recon.run", "tools.suiteql", "schedules.manage", "approvals.manage",
          "chat.financial_reports"],
  finance: ["connections.view", "tables.view", "audit.view", "exports.csv",
            "exports.excel", "recon.run", "tools.suiteql", "chat.financial_reports"],
  ops: ["connections.manage", "connections.view", "tables.view", "audit.view",
        "exports.csv", "schedules.manage"],
  readonly: ["connections.view", "tables.view", "audit.view"],
};

export function usePermissions() {
  const { user } = useAuth();

  const permissions = useMemo(() => {
    if (!user?.roles?.length) return new Set<string>();
    const perms = new Set<string>();
    for (const role of user.roles) {
      for (const p of ROLE_PERMISSIONS[role] ?? []) perms.add(p);
    }
    return perms;
  }, [user?.roles]);

  const hasPermission = useCallback(
    (codename: string) => permissions.has(codename),
    [permissions]
  );

  const isAdmin = useMemo(
    () => user?.roles?.includes("admin") ?? false,
    [user?.roles]
  );

  return { hasPermission, isAdmin, permissions };
}
```

### New frontend hooks

**File:** `frontend/src/hooks/use-team.ts`

```typescript
"use client";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";

// List team members
export function useTeamMembers() {
  return useQuery<TeamMember[]>({
    queryKey: ["team", "members"],
    queryFn: () => apiClient.get("/api/v1/users"),
  });
}

// List invites
export function useTeamInvites() {
  return useQuery<Invite[]>({
    queryKey: ["team", "invites"],
    queryFn: () => apiClient.get("/api/v1/invites"),
  });
}

// Send invite
export function useCreateInvite() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: { email: string; role_name: RoleName }) =>
      apiClient.post("/api/v1/invites", data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["team"] }),
  });
}

// Revoke invite
export function useRevokeInvite() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (inviteId: string) =>
      apiClient.delete(`/api/v1/invites/${inviteId}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["team"] }),
  });
}

// Change user role
export function useChangeUserRole() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ userId, roles }: { userId: string; roles: string[] }) =>
      apiClient.patch(`/api/v1/users/${userId}/roles`, { roles }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["team"] }),
  });
}

// Deactivate user
export function useDeactivateUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (userId: string) =>
      apiClient.delete(`/api/v1/users/${userId}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["team"] }),
  });
}
```

### Settings page — Team tab

**File:** `frontend/src/app/(dashboard)/settings/page.tsx`

Add a new tab alongside existing settings sections. Only visible to users with `users.manage` permission.

**Tab content layout:**

```
┌─────────────────────────────────────────────────────┐
│  Team                                                │
│  Manage your team members and invitations            │
│                                                      │
│  [Invite Team Member]  button (top right)            │
│                                                      │
│  Members (5 of 20)                                   │
│  ┌─────────────────────────────────────────────────┐ │
│  │ Avatar  Jane Doe          Admin      Active     │ │
│  │         jane@co.com       (you)                 │ │
│  │ Avatar  Bob Smith         User       Active  ⋮  │ │
│  │         bob@co.com                              │ │
│  │ Avatar  Sue Park          Ops Only   Active  ⋮  │ │
│  │         sue@co.com                              │ │
│  └─────────────────────────────────────────────────┘ │
│                                                      │
│  Pending Invitations (2)                             │
│  ┌─────────────────────────────────────────────────┐ │
│  │ ✉  mike@co.com    User     Sent Mar 14   [X]   │ │
│  │ ✉  lin@co.com     Ops Only Sent Mar 15   [X]   │ │
│  └─────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────┘
```

**⋮ kebab menu actions per member:**
- Change role → dropdown (Admin / User / Operations Only)
- Deactivate → confirm dialog

**Invite dialog:**
- Email input (validated)
- Role dropdown: Admin / User / Operations Only
- "Send Invite" button
- Shows seat count: "3 of 20 seats used"

**Rules:**
- Cannot change your own role
- Cannot deactivate yourself
- Cannot deactivate or demote the last admin (backend enforces, frontend shows disabled state)
- "(you)" badge next to current user
- Seat count shows `active_users + pending_invites` vs `max_users`
- Non-admin users see a read-only team list (who's on the team, no actions)

### Invite acceptance page

**File:** `frontend/src/app/invite/[token]/page.tsx`

Public page (no auth required). Layout:

```
┌──────────────────────────────────────────────┐
│            [Tenant Logo]                      │
│                                               │
│   Jane Doe invited you to join               │
│   Acme Corp on SuiteStudio                   │
│                                               │
│   Role: User                                 │
│                                               │
│   ┌──────────────────────────────────────┐   │
│   │  Full Name  [____________]            │   │
│   │                                       │   │
│   │  ── Sign up with ──                   │   │
│   │                                       │   │
│   │  [🔵 Continue with Google]            │   │
│   │                                       │   │
│   │  ── or ──                             │   │
│   │                                       │   │
│   │  Password    [____________]            │   │
│   │  Confirm     [____________]            │   │
│   │                                       │   │
│   │  [Create Account]                     │   │
│   └──────────────────────────────────────┘   │
│                                               │
│   Already have an account? [Log in]          │
└──────────────────────────────────────────────┘
```

**States:**
- Loading → show skeleton
- Valid invite → show form above
- Expired invite → "This invitation has expired. Contact your admin for a new one."
- Already accepted → "This invitation has already been used." + login link
- Invalid token → "Invalid invitation link."

**Google Sign-In flow:**
1. User clicks "Continue with Google"
2. Google Sign-In popup opens (using `@react-oauth/google` or Google Identity Services)
3. Returns `id_token` (JWT from Google)
4. Frontend sends `{ full_name, google_id_token }` to `POST /invites/accept/{token}`
5. Backend verifies Google token, checks email matches invite, creates user
6. Returns JWT tokens → frontend stores + redirects to dashboard

**New dependency:** `@react-oauth/google` in `frontend/package.json`

### Login page update

**File:** `frontend/src/app/login/page.tsx` (or equivalent)

Add "Sign in with Google" button below the existing email/password form:

```
  [email field]
  [password field]
  [Sign In]

  ── or ──

  [🔵 Sign in with Google]
```

This calls `POST /auth/google` with the Google ID token. Only works for users who signed up via Google (returns 404 otherwise, frontend shows "No account found with this Google account. Ask your admin for an invite.").

### Hide finance features for Operations Only

Use the `usePermissions()` hook to conditionally render:

| Component | Gate | Behavior when denied |
|-----------|------|---------------------|
| Financial report tool card | `chat.financial_reports` | Hidden from chat results |
| Financial report in message list | `chat.financial_reports` | Hidden |
| Finance-related settings | `chat.financial_reports` | Hidden from settings |
| Soul.md editor | `tenant.manage` | Hidden / read-only |
| "Remember" button | `tenant.manage` | Hidden |
| Reporting tier dropdown | `tenant.manage` | Hidden |
| Team tab in Settings | `users.manage` | Hidden (or read-only member list) |
| Connection management | `connections.manage` | View-only for non-admin/ops |

---

## Testing

### Unit tests: `backend/tests/test_invite_service.py`

| Test | Assertion |
|------|-----------|
| `test_create_invite_success` | Invite created with correct token, expiry, role |
| `test_create_invite_duplicate_email` | 400 if pending invite already exists for email |
| `test_create_invite_existing_user` | 400 if email already active in tenant |
| `test_create_invite_seat_limit` | 400 if active users + pending invites ≥ max_users |
| `test_create_invite_invalid_role` | 400 for non-existent role name |
| `test_accept_invite_password` | User created with hashed password, role assigned, invite marked accepted |
| `test_accept_invite_google` | User created with google_sub, auth_provider="google" |
| `test_accept_expired_invite` | 400 with "invitation expired" message |
| `test_accept_already_accepted` | 400 with "already accepted" message |
| `test_revoke_invite` | Status set to "revoked" |
| `test_last_admin_protection` | 400 when demoting or deactivating last admin |

### Integration tests: `backend/tests/test_invites_api.py`

| Test | Assertion |
|------|-----------|
| `test_create_invite_requires_permission` | 403 without users.manage |
| `test_create_invite_returns_201` | 201 with invite details |
| `test_list_invites_returns_pending` | Only pending/accepted, not expired/revoked |
| `test_accept_invite_returns_tokens` | 200 with access_token + refresh_token |
| `test_accept_invite_public_endpoint` | No auth required |
| `test_get_invite_info_public` | Returns tenant name, email, role, expiry |
| `test_revoke_invite_returns_204` | 204 on success |
| `test_google_login_existing_user` | 200 with tokens for Google-authed user |
| `test_google_login_unknown_email` | 404 for non-existent user |

### Integration tests: `backend/tests/test_financial_permission.py`

| Test | Assertion |
|------|-----------|
| `test_admin_can_access_financial_reports` | Financial mode activates for admin |
| `test_user_can_access_financial_reports` | Financial mode activates for finance role |
| `test_ops_blocked_from_financial_reports` | Agent returns permission denied message |

---

## Data Flow

### Invite flow
```
Admin clicks "Invite Team Member"
    ↓
Fills email + selects role → POST /api/v1/invites
    ↓
Backend: validate seat limit → create Invite row → send email
    ↓
Invitee clicks email link → /invite/{token}
    ↓
Frontend: GET /invites/accept/{token} → show invite info
    ↓
Invitee fills name + (password OR Google) → POST /invites/accept/{token}
    ↓
Backend: verify → create User + UserRole → return JWT tokens
    ↓
Frontend: store tokens → redirect to dashboard
```

### Permission check flow (financial reports)
```
User sends chat message
    ↓
Orchestrator: detect financial intent
    ↓
Check has_permission(user, "chat.financial_reports")
    ↓
Yes → activate financial mode, call netsuite_financial_report tool
No  → inject "Financial reports restricted" into agent response (no tool call)
```

---

## Files Changed

| File | Change |
|------|--------|
| `backend/app/models/invite.py` | **New** — Invite model |
| `backend/app/services/invite_service.py` | **New** — Invite CRUD + email + seat limits |
| `backend/app/services/google_auth_service.py` | **New** — Google ID token verification |
| `backend/app/services/email_service.py` | **New** — Transactional email (invite, welcome) |
| `backend/app/api/v1/invites.py` | **New** — Invite endpoints (5 endpoints) |
| `backend/app/api/v1/auth.py` | Add `POST /auth/google` endpoint |
| `backend/app/api/v1/users.py` | Add last-admin protection, seat limit check |
| `backend/app/api/v1/router.py` | Register invites router |
| `backend/app/services/entitlement_service.py` | Add `max_users` to plan features |
| `backend/app/models/user.py` | Add `auth_provider`, `google_sub` fields |
| `backend/app/services/chat/orchestrator.py` | Check `chat.financial_reports` permission before financial mode |
| `backend/pyproject.toml` | Add `google-auth` dependency |
| `backend/alembic/versions/043_chat_financial_permission.py` | **New** — Add permission + assign to roles |
| `backend/alembic/versions/044_invites_table.py` | **New** — Invites table |
| `backend/alembic/versions/045_user_google_auth.py` | **New** — auth_provider + google_sub columns |
| `frontend/src/lib/types.ts` | Fix Role type to match backend |
| `frontend/src/hooks/use-permissions.ts` | **New** — Permission helper hook |
| `frontend/src/hooks/use-team.ts` | **New** — Team management hooks |
| `frontend/src/app/(dashboard)/settings/page.tsx` | Add Team tab |
| `frontend/src/app/invite/[token]/page.tsx` | **New** — Invite acceptance page |
| `frontend/src/app/login/page.tsx` | Add Google Sign-In button |
| `frontend/src/providers/auth-provider.tsx` | Add `googleLogin()` method |
| `frontend/package.json` | Add `@react-oauth/google` |
| `backend/tests/test_invite_service.py` | **New** — 11 unit tests |
| `backend/tests/test_invites_api.py` | **New** — 9 integration tests |
| `backend/tests/test_financial_permission.py` | **New** — 3 permission tests |

## Migration Order

Migrations must run in this order due to dependencies:
1. `043_chat_financial_permission.py` — standalone, no model changes
2. `044_invites_table.py` — creates invites table
3. `045_user_google_auth.py` — adds columns to users table

## Environment Variables

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `GOOGLE_CLIENT_ID` | Phase 2+ | None | Google OAuth Web Client ID |
| `EMAIL_PROVIDER` | Phase 1+ | `console` | Email provider: `resend`, `sendgrid`, `ses`, `console` (prints to stdout in dev) |
| `EMAIL_API_KEY` | Production | None | API key for email provider |
| `EMAIL_FROM_ADDRESS` | Production | `noreply@suitestudio.app` | Sender address |
| `FRONTEND_URL` | Phase 1+ | `http://localhost:3000` | Used in invite email links |

## Open Questions (Resolved)

- **Why not per-permission granularity?** Three fixed roles are simpler to reason about and match the user's stated needs. The underlying permission system supports granularity if needed later — no migration required, just UI work.
- **Why map to existing backend roles instead of creating new ones?** Avoids a migration to rename roles and re-seed data. The display name mapping lives in the frontend only.
- **Why `console` email provider for dev?** Prints invite link to stdout so developers can test without configuring an email service. Easy to swap for Resend/SendGrid in staging/production via env var.
- **Seat counting includes pending invites?** Yes — prevents admins from sending 50 invites on a 20-seat plan. Expired/revoked invites don't count.
