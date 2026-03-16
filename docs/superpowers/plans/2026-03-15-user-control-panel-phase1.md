# User Control Panel & RBAC — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add team management UI in Settings, invite flow with email, role assignment (Admin / User / Operations Only), seat limits, and financial report permission gating.

**Architecture:** Leverage existing RBAC foundation (4 roles, 12 permissions, UserRole join table). Add Invite model for email-based invites with token + expiry. Add `chat.financial_reports` permission to gate financial reports for ops users. Frontend gets a Team section in Settings page + invite acceptance page. Google Sign-In stubbed for Phase 2.

**Tech Stack:** FastAPI, SQLAlchemy, Pydantic, secrets.token_urlsafe, console email (dev), React, shadcn/ui, lucide-react

**Spec:** `docs/superpowers/specs/2026-03-15-user-control-panel-rbac.md`

**Phase 1 scope:** Invite flow, Team UI, seat limits, permission gating. Google Sign-In deferred to Phase 2 (auth_provider field stubbed as "email").

---

## File Structure

| File | Responsibility |
|------|---------------|
| `backend/alembic/versions/044_financial_permission.py` | New permission + role assignment |
| `backend/alembic/versions/045_invites_table.py` | Invites table |
| `backend/alembic/versions/046_user_auth_provider.py` | auth_provider + google_sub on users |
| `backend/app/models/invite.py` | Invite model |
| `backend/app/models/user.py` | Add auth_provider, google_sub fields |
| `backend/app/services/invite_service.py` | Invite CRUD, seat limit check, token generation |
| `backend/app/services/email_service.py` | Email abstraction (console provider for dev) |
| `backend/app/services/entitlement_service.py` | Add max_users to plan limits |
| `backend/app/api/v1/invites.py` | 5 invite endpoints |
| `backend/app/api/v1/users.py` | Last-admin protection |
| `backend/app/api/v1/router.py` | Register invites router |
| `backend/app/services/chat/orchestrator.py` | Financial permission check |
| `frontend/src/lib/types.ts` | Fix Role type, add auth_provider |
| `frontend/src/hooks/use-permissions.ts` | Permission helper hook |
| `frontend/src/hooks/use-team.ts` | Team management hooks |
| `frontend/src/components/settings/team-section.tsx` | Team section component |
| `frontend/src/app/(dashboard)/settings/page.tsx` | Add Team section |
| `frontend/src/app/invite/[token]/page.tsx` | Invite acceptance page |
| `backend/tests/test_invite_service.py` | Unit tests |
| `backend/tests/test_invites_api.py` | Integration tests |

---

## Chunk 1: Backend — Models, Migrations, Services

### Task 1: Migration 044 — chat.financial_reports permission

**Files:**
- Create: `backend/alembic/versions/044_financial_permission.py`

- [ ] **Step 1: Create migration**

```python
"""Add chat.financial_reports permission and assign to admin + finance roles."""

from alembic import op

revision = "044_financial_perm"
down_revision = "043_saved_query_data"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO permissions (id, codename)
        VALUES (gen_random_uuid(), 'chat.financial_reports')
    """)
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

- [ ] **Step 2: Verify migration applies**

Run: `cd backend && docker exec ecom-netsuite-suites-backend-1 alembic upgrade head`

- [ ] **Step 3: Commit**

```bash
git add backend/alembic/versions/044_financial_permission.py
git commit -m "feat: add chat.financial_reports permission (admin + finance only)"
```

---

### Task 2: Migration 045 — Invites table + Migration 046 — User auth_provider

**Files:**
- Create: `backend/alembic/versions/045_invites_table.py`
- Create: `backend/alembic/versions/046_user_auth_provider.py`
- Create: `backend/app/models/invite.py`
- Modify: `backend/app/models/user.py:24-28`

- [ ] **Step 1: Create Invite model**

Create `backend/app/models/invite.py`:

```python
"""Invite model for team member invitations."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Invite(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "invites"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    role_name: Mapped[str] = mapped_column(String(50), nullable=False, default="finance")
    invited_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # status: "pending" | "accepted" | "expired" | "revoked"
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # Only one pending invite per email per tenant (revoked/accepted don't count)
        UniqueConstraint("tenant_id", "email", "status", name="uq_invite_tenant_email_pending"),
    )
```

- [ ] **Step 2: Add auth_provider and google_sub to User model**

In `backend/app/models/user.py`, add after the `global_role` field (line 28):

```python
    auth_provider: Mapped[str] = mapped_column(String(20), default="email", nullable=False, server_default="email")
    google_sub: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
```

- [ ] **Step 3: Create migration 045 — invites table**

```python
"""Create invites table for team member invitations."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "045_invites_table"
down_revision = "044_financial_perm"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invites",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False, index=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("role_name", sa.String(50), nullable=False, server_default="finance"),
        sa.Column("invited_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("token", sa.String(64), nullable=False, unique=True, index=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("tenant_id", "email", "status", name="uq_invite_tenant_email_pending"),
    )


def downgrade() -> None:
    op.drop_table("invites")
```

- [ ] **Step 4: Create migration 046 — user auth_provider**

```python
"""Add auth_provider and google_sub columns to users."""

from alembic import op
import sqlalchemy as sa

revision = "046_user_auth_provider"
down_revision = "045_invites_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("auth_provider", sa.String(20), nullable=False, server_default="email"))
    op.add_column("users", sa.Column("google_sub", sa.String(255), nullable=True))
    op.create_unique_constraint("uq_users_google_sub", "users", ["google_sub"])


def downgrade() -> None:
    op.drop_constraint("uq_users_google_sub", "users")
    op.drop_column("users", "google_sub")
    op.drop_column("users", "auth_provider")
```

- [ ] **Step 5: Run migrations**

Run: `cd backend && docker exec ecom-netsuite-suites-backend-1 alembic upgrade head`

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/invite.py backend/app/models/user.py backend/alembic/versions/045_invites_table.py backend/alembic/versions/046_user_auth_provider.py
git commit -m "feat: add Invite model, auth_provider field, and migrations 045-046"
```

---

### Task 3: Email service + Invite service — tests first

**Files:**
- Create: `backend/app/services/email_service.py`
- Create: `backend/app/services/invite_service.py`
- Create: `backend/tests/test_invite_service.py`
- Modify: `backend/app/services/entitlement_service.py:13-50`

- [ ] **Step 1: Add max_users to entitlement service**

In `backend/app/services/entitlement_service.py`, add `"max_users"` to each plan in `PLAN_LIMITS`:

```python
PLAN_LIMITS = {
    "free": {
        # ... existing fields ...
        "max_users": 20,
    },
    "pro": {
        # ... existing fields ...
        "max_users": 50,
    },
    "max": {
        # ... existing fields ...
        "max_users": 999,
    },
}
```

And add a handler in `check_entitlement()` (after the schedules handler):

```python
    if feature == "users":
        from app.models.user import User
        count_result = await db.execute(
            select(func.count(User.id)).where(
                User.tenant_id == tenant_id,
                User.is_active.is_(True),
            )
        )
        current_count = count_result.scalar() or 0
        max_allowed = limits["max_users"]
        if max_allowed == -1:
            return True
        return current_count < max_allowed
```

Also add `users` to `get_usage_summary()`:

```python
    from app.models.user import User
    user_result = await db.execute(
        select(func.count(User.id)).where(
            User.tenant_id == tenant_id,
            User.is_active.is_(True),
        )
    )
    users = user_result.scalar() or 0

    return {
        "connections": connections,
        "schedules": schedules,
        "users": users,
    }
```

- [ ] **Step 2: Create email service**

Create `backend/app/services/email_service.py`:

```python
"""Email service — console provider for dev, extensible for production."""

import os

import structlog

logger = structlog.get_logger()

EMAIL_PROVIDER = os.environ.get("EMAIL_PROVIDER", "console")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")


async def send_invite_email(
    *,
    to_email: str,
    inviter_name: str,
    tenant_brand_name: str,
    role_display_name: str,
    token: str,
) -> None:
    """Send an invite email. In dev mode, prints to console."""
    accept_url = f"{FRONTEND_URL}/invite/{token}"

    subject = f"{inviter_name} invited you to {tenant_brand_name} on SuiteStudio"
    body = (
        f"{inviter_name} has invited you to join {tenant_brand_name} "
        f"on SuiteStudio as a {role_display_name}.\n\n"
        f"Accept your invitation: {accept_url}\n\n"
        f"This invitation expires in 7 days."
    )

    if EMAIL_PROVIDER == "console":
        print(f"\n{'='*60}", flush=True)
        print(f"INVITE EMAIL (console mode)", flush=True)
        print(f"To: {to_email}", flush=True)
        print(f"Subject: {subject}", flush=True)
        print(f"Body:\n{body}", flush=True)
        print(f"Accept URL: {accept_url}", flush=True)
        print(f"{'='*60}\n", flush=True)
        return

    # Production email providers (Phase 2+)
    logger.info("email.send", provider=EMAIL_PROVIDER, to=to_email, subject=subject)
    raise NotImplementedError(f"Email provider '{EMAIL_PROVIDER}' not yet implemented")
```

- [ ] **Step 3: Write invite service tests**

Create `backend/tests/test_invite_service.py`:

```python
"""Tests for invite_service."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.invite import Invite
from app.models.user import User
from app.services.invite_service import (
    ROLE_DISPLAY_NAMES,
    VALID_INVITE_ROLES,
    accept_invite,
    create_invite,
    list_invites,
    revoke_invite,
)


@pytest.fixture
def tenant_id():
    return uuid.uuid4()


@pytest.fixture
def user_id():
    return uuid.uuid4()


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    db.add = MagicMock()
    return db


class TestCreateInvite:
    @pytest.mark.asyncio
    async def test_invalid_role_raises(self, mock_db, tenant_id, user_id):
        with pytest.raises(ValueError, match="Invalid role"):
            await create_invite(
                db=mock_db,
                tenant_id=tenant_id,
                email="test@example.com",
                role_name="nonexistent",
                invited_by=user_id,
                inviter_name="Admin",
                tenant_brand_name="Acme",
            )

    def test_valid_roles(self):
        assert "admin" in VALID_INVITE_ROLES
        assert "finance" in VALID_INVITE_ROLES
        assert "ops" in VALID_INVITE_ROLES
        assert "readonly" not in VALID_INVITE_ROLES

    def test_role_display_names(self):
        assert ROLE_DISPLAY_NAMES["admin"] == "Admin"
        assert ROLE_DISPLAY_NAMES["finance"] == "User"
        assert ROLE_DISPLAY_NAMES["ops"] == "Operations Only"


class TestAcceptInvite:
    @pytest.mark.asyncio
    async def test_expired_invite_raises(self, mock_db):
        expired_invite = MagicMock(spec=Invite)
        expired_invite.status = "pending"
        expired_invite.expires_at = datetime.now(timezone.utc) - timedelta(days=1)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = expired_invite
        mock_db.execute = AsyncMock(return_value=mock_result)

        with pytest.raises(ValueError, match="expired"):
            await accept_invite(
                db=mock_db,
                token="test-token",
                full_name="Test User",
                password="TestPass1!",
            )

    @pytest.mark.asyncio
    async def test_already_accepted_raises(self, mock_db):
        accepted_invite = MagicMock(spec=Invite)
        accepted_invite.status = "accepted"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = accepted_invite
        mock_db.execute = AsyncMock(return_value=mock_result)

        with pytest.raises(ValueError, match="already been accepted"):
            await accept_invite(
                db=mock_db,
                token="test-token",
                full_name="Test User",
                password="TestPass1!",
            )


class TestRevokeInvite:
    @pytest.mark.asyncio
    async def test_revoke_sets_status(self, mock_db, tenant_id):
        invite = MagicMock(spec=Invite)
        invite.status = "pending"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = invite
        mock_db.execute = AsyncMock(return_value=mock_result)

        await revoke_invite(mock_db, uuid.uuid4(), tenant_id)
        assert invite.status == "revoked"
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `cd backend && .venv/bin/python -m pytest tests/test_invite_service.py -v 2>&1 | head -10`
Expected: ImportError — module `app.services.invite_service` not found

- [ ] **Step 5: Implement invite service**

Create `backend/app/services/invite_service.py`:

```python
"""Invite service — create, accept, revoke, list invites."""

import secrets
import uuid
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.invite import Invite
from app.models.user import Role, User, UserRole

logger = structlog.get_logger()

VALID_INVITE_ROLES = {"admin", "finance", "ops"}

ROLE_DISPLAY_NAMES = {
    "admin": "Admin",
    "finance": "User",
    "ops": "Operations Only",
    "readonly": "Read Only",
}

INVITE_EXPIRY_DAYS = 7


async def create_invite(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    email: str,
    role_name: str,
    invited_by: uuid.UUID,
    inviter_name: str,
    tenant_brand_name: str,
) -> Invite:
    """Create an invite and send email."""
    from app.services.email_service import send_invite_email

    if role_name not in VALID_INVITE_ROLES:
        raise ValueError(f"Invalid role: {role_name}. Must be one of {VALID_INVITE_ROLES}")

    # Check email not already an active user in this tenant
    existing_user = await db.execute(
        select(User).where(
            User.tenant_id == tenant_id,
            User.email == email,
            User.is_active.is_(True),
        )
    )
    if existing_user.scalar_one_or_none():
        raise ValueError("A user with this email already exists in your team.")

    # Check no pending invite for same email
    existing_invite = await db.execute(
        select(Invite).where(
            Invite.tenant_id == tenant_id,
            Invite.email == email,
            Invite.status == "pending",
        )
    )
    if existing_invite.scalar_one_or_none():
        raise ValueError("A pending invite already exists for this email.")

    # Check seat limit
    user_count = await db.execute(
        select(func.count(User.id)).where(
            User.tenant_id == tenant_id,
            User.is_active.is_(True),
        )
    )
    invite_count = await db.execute(
        select(func.count(Invite.id)).where(
            Invite.tenant_id == tenant_id,
            Invite.status == "pending",
        )
    )
    total = (user_count.scalar() or 0) + (invite_count.scalar() or 0)

    from app.services.entitlement_service import get_plan_limits
    limits = await get_plan_limits(db, tenant_id)
    max_users = limits.get("max_users", 20)
    if max_users != -1 and total >= max_users:
        raise ValueError(f"Seat limit reached ({max_users}). Upgrade your plan to invite more users.")

    token = secrets.token_urlsafe(32)
    invite = Invite(
        tenant_id=tenant_id,
        email=email,
        role_name=role_name,
        invited_by=invited_by,
        token=token,
        status="pending",
        expires_at=datetime.now(timezone.utc) + timedelta(days=INVITE_EXPIRY_DAYS),
    )
    db.add(invite)
    await db.flush()

    # Send email (fire-and-forget in dev, async in prod)
    await send_invite_email(
        to_email=email,
        inviter_name=inviter_name,
        tenant_brand_name=tenant_brand_name,
        role_display_name=ROLE_DISPLAY_NAMES.get(role_name, role_name),
        token=token,
    )

    return invite


async def accept_invite(
    db: AsyncSession,
    token: str,
    full_name: str,
    password: str | None = None,
    google_id_token: str | None = None,
) -> tuple[User, dict]:
    """Accept an invite — creates user account, assigns role, returns JWT tokens."""
    from app.core.security import create_access_token, create_refresh_token

    result = await db.execute(
        select(Invite).where(Invite.token == token)
    )
    invite = result.scalar_one_or_none()
    if not invite:
        raise ValueError("Invalid invitation link.")

    if invite.status == "accepted":
        raise ValueError("This invitation has already been accepted.")

    if invite.status == "revoked":
        raise ValueError("This invitation has been revoked.")

    if invite.status == "expired" or invite.expires_at < datetime.now(timezone.utc):
        invite.status = "expired"
        raise ValueError("This invitation has expired. Contact your admin for a new one.")

    if not password and not google_id_token:
        raise ValueError("Either password or Google sign-in is required.")

    # Create user
    auth_provider = "email"
    hashed_pw = hash_password(password) if password else hash_password(secrets.token_urlsafe(32))

    if google_id_token:
        # Phase 2: verify Google token, extract email, match
        raise ValueError("Google Sign-In is not yet available. Please use a password.")

    new_user = User(
        tenant_id=invite.tenant_id,
        email=invite.email,
        hashed_password=hashed_pw,
        full_name=full_name,
        auth_provider=auth_provider,
    )
    db.add(new_user)
    await db.flush()

    # Assign role
    role_result = await db.execute(select(Role).where(Role.name == invite.role_name))
    role = role_result.scalar_one_or_none()
    if role:
        db.add(UserRole(tenant_id=invite.tenant_id, user_id=new_user.id, role_id=role.id))

    # Mark invite as accepted
    invite.status = "accepted"
    invite.accepted_at = datetime.now(timezone.utc)

    await db.flush()

    # Generate tokens
    tokens = {
        "access_token": create_access_token(str(new_user.id)),
        "refresh_token": create_refresh_token(str(new_user.id)),
    }

    return new_user, tokens


async def revoke_invite(
    db: AsyncSession,
    invite_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> None:
    """Revoke a pending invite."""
    result = await db.execute(
        select(Invite).where(
            Invite.id == invite_id,
            Invite.tenant_id == tenant_id,
        )
    )
    invite = result.scalar_one_or_none()
    if not invite:
        raise ValueError("Invite not found.")
    if invite.status != "pending":
        raise ValueError("Only pending invites can be revoked.")
    invite.status = "revoked"


async def list_invites(
    db: AsyncSession,
    tenant_id: uuid.UUID,
) -> list[Invite]:
    """List pending + accepted invites for tenant."""
    result = await db.execute(
        select(Invite).where(
            Invite.tenant_id == tenant_id,
            Invite.status.in_(["pending", "accepted"]),
        ).order_by(Invite.created_at.desc())
    )
    return list(result.scalars().all())


async def get_invite_by_token(
    db: AsyncSession,
    token: str,
) -> Invite | None:
    """Get invite by token (public lookup)."""
    result = await db.execute(
        select(Invite).where(Invite.token == token)
    )
    return result.scalar_one_or_none()
```

- [ ] **Step 6: Run tests**

Run: `cd backend && .venv/bin/python -m pytest tests/test_invite_service.py -v`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/email_service.py backend/app/services/invite_service.py backend/app/services/entitlement_service.py backend/tests/test_invite_service.py
git commit -m "feat: add invite service, email service, and seat limit enforcement"
```

---

### Task 4: Invite API endpoints — tests first

**Files:**
- Create: `backend/app/api/v1/invites.py`
- Create: `backend/tests/test_invites_api.py`
- Modify: `backend/app/api/v1/router.py`

- [ ] **Step 1: Write integration tests**

Create `backend/tests/test_invites_api.py`:

```python
"""Tests for invite API endpoints."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.dependencies import get_current_user
from app.core.database import get_db
from app.main import app
from app.models.invite import Invite


@pytest.fixture
def mock_admin():
    user = MagicMock()
    user.id = uuid.uuid4()
    user.tenant_id = uuid.uuid4()
    user.email = "admin@example.com"
    user.full_name = "Admin User"
    ur = MagicMock()
    ur.role_id = uuid.uuid4()
    ur.role = MagicMock()
    ur.role.name = "admin"
    user.user_roles = [ur]
    return user


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    return db


@pytest.fixture(autouse=True)
def override_deps(mock_admin, mock_db):
    app.dependency_overrides[get_current_user] = lambda: mock_admin
    app.dependency_overrides[get_db] = lambda: mock_db
    yield
    app.dependency_overrides.clear()


class TestCreateInvite:
    @pytest.mark.asyncio
    async def test_returns_201(self, mock_admin, mock_db):
        with patch("app.api.v1.invites.invite_service") as mock_svc:
            invite = MagicMock(spec=Invite)
            invite.id = uuid.uuid4()
            invite.email = "new@example.com"
            invite.role_name = "finance"
            invite.status = "pending"
            invite.expires_at = datetime.now(timezone.utc) + timedelta(days=7)
            invite.created_at = datetime.now(timezone.utc)
            mock_svc.create_invite = AsyncMock(return_value=invite)

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/invites",
                    json={"email": "new@example.com", "role_name": "finance"},
                    headers={"Authorization": "Bearer test"},
                )
            assert response.status_code == 201

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app.dependency_overrides.clear()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/invites",
                json={"email": "new@example.com", "role_name": "finance"},
            )
        assert response.status_code in (401, 403)


class TestAcceptInvite:
    @pytest.mark.asyncio
    async def test_public_endpoint(self, mock_db):
        """Accept endpoint should not require auth."""
        app.dependency_overrides.clear()
        app.dependency_overrides[get_db] = lambda: mock_db

        with patch("app.api.v1.invites.invite_service") as mock_svc:
            user = MagicMock()
            user.id = uuid.uuid4()
            user.tenant_id = uuid.uuid4()
            user.email = "new@example.com"
            user.full_name = "New User"
            tokens = {"access_token": "test-access", "refresh_token": "test-refresh"}
            mock_svc.accept_invite = AsyncMock(return_value=(user, tokens))

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/invites/accept/test-token-123",
                    json={"full_name": "New User", "password": "TestPass1!"},
                )
            assert response.status_code == 200
            data = response.json()
            assert "access_token" in data


class TestGetInviteInfo:
    @pytest.mark.asyncio
    async def test_returns_invite_details(self, mock_db):
        """Get invite info is public — returns tenant name and role."""
        app.dependency_overrides.clear()
        app.dependency_overrides[get_db] = lambda: mock_db

        with patch("app.api.v1.invites.invite_service") as mock_svc:
            invite = MagicMock(spec=Invite)
            invite.email = "new@example.com"
            invite.role_name = "finance"
            invite.status = "pending"
            invite.expires_at = datetime.now(timezone.utc) + timedelta(days=5)
            invite.tenant_id = uuid.uuid4()
            mock_svc.get_invite_by_token = AsyncMock(return_value=invite)

            with patch("app.api.v1.invites._get_tenant_name", return_value="Acme Corp"):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                    response = await client.get("/api/v1/invites/accept/test-token")
                assert response.status_code == 200
                data = response.json()
                assert data["email"] == "new@example.com"
                assert data["role_display_name"] == "User"
```

- [ ] **Step 2: Implement the invites router**

Create `backend/app/api/v1/invites.py`:

```python
"""Invite endpoints for team member management."""

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_permission
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit_service, invite_service
from app.services.invite_service import ROLE_DISPLAY_NAMES

router = APIRouter(prefix="/invites", tags=["invites"])


# --- Schemas ---

class InviteCreate(BaseModel):
    email: EmailStr
    role_name: str = Field(default="finance", pattern=r"^(admin|finance|ops)$")


class InviteResponse(BaseModel):
    id: str
    email: str
    role_name: str
    role_display_name: str
    status: str
    expires_at: str
    created_at: str


class InviteAcceptRequest(BaseModel):
    full_name: str = Field(min_length=1, max_length=255)
    password: str | None = Field(default=None, min_length=8, max_length=128)
    google_id_token: str | None = None


class InviteAcceptInfo(BaseModel):
    email: str
    role_name: str
    role_display_name: str
    tenant_name: str
    status: str
    expired: bool


class AuthResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


# --- Helpers ---

async def _get_tenant_name(db: AsyncSession, tenant_id: uuid.UUID) -> str:
    result = await db.execute(select(Tenant.name).where(Tenant.id == tenant_id))
    return result.scalar_one_or_none() or "Unknown"


# --- Endpoints ---

@router.post("", response_model=InviteResponse, status_code=status.HTTP_201_CREATED)
async def create_invite_endpoint(
    request: InviteCreate,
    user: Annotated[User, Depends(require_permission("users.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Send an invite. Requires users.manage permission (Admin only)."""
    tenant_name = await _get_tenant_name(db, user.tenant_id)

    try:
        invite = await invite_service.create_invite(
            db=db,
            tenant_id=user.tenant_id,
            email=request.email,
            role_name=request.role_name,
            invited_by=user.id,
            inviter_name=user.full_name,
            tenant_brand_name=tenant_name,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="invite",
        action="invite.create",
        actor_id=user.id,
        resource_type="invite",
        resource_id=str(invite.id),
    )
    await db.commit()
    await db.refresh(invite)

    return InviteResponse(
        id=str(invite.id),
        email=invite.email,
        role_name=invite.role_name,
        role_display_name=ROLE_DISPLAY_NAMES.get(invite.role_name, invite.role_name),
        status=invite.status,
        expires_at=invite.expires_at.isoformat(),
        created_at=invite.created_at.isoformat(),
    )


@router.get("", response_model=list[InviteResponse])
async def list_invites_endpoint(
    user: Annotated[User, Depends(require_permission("users.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """List all invites for current tenant."""
    invites = await invite_service.list_invites(db, user.tenant_id)
    return [
        InviteResponse(
            id=str(inv.id),
            email=inv.email,
            role_name=inv.role_name,
            role_display_name=ROLE_DISPLAY_NAMES.get(inv.role_name, inv.role_name),
            status=inv.status,
            expires_at=inv.expires_at.isoformat(),
            created_at=inv.created_at.isoformat(),
        )
        for inv in invites
    ]


@router.delete("/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invite_endpoint(
    invite_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("users.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Revoke a pending invite."""
    try:
        await invite_service.revoke_invite(db, invite_id, user.tenant_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="invite",
        action="invite.revoke",
        actor_id=user.id,
        resource_type="invite",
        resource_id=str(invite_id),
    )
    await db.commit()


@router.get("/accept/{token}", response_model=InviteAcceptInfo)
async def get_invite_info(
    token: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Public endpoint — returns invite details for the accept page."""
    invite = await invite_service.get_invite_by_token(db, token)
    if not invite:
        raise HTTPException(status_code=404, detail="Invalid invitation link.")

    tenant_name = await _get_tenant_name(db, invite.tenant_id)
    expired = invite.expires_at < datetime.now(timezone.utc)

    return InviteAcceptInfo(
        email=invite.email,
        role_name=invite.role_name,
        role_display_name=ROLE_DISPLAY_NAMES.get(invite.role_name, invite.role_name),
        tenant_name=tenant_name,
        status=invite.status,
        expired=expired,
    )


@router.post("/accept/{token}", response_model=AuthResponse)
async def accept_invite_endpoint(
    token: str,
    request: InviteAcceptRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Public endpoint — accepts invite, creates account, returns JWT tokens."""
    try:
        user, tokens = await invite_service.accept_invite(
            db=db,
            token=token,
            full_name=request.full_name,
            password=request.password,
            google_id_token=request.google_id_token,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="invite",
        action="invite.accept",
        actor_id=user.id,
        resource_type="user",
        resource_id=str(user.id),
    )
    await db.commit()

    return AuthResponse(
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
    )
```

- [ ] **Step 3: Register the router**

In `backend/app/api/v1/router.py`, add `invites` to imports and `api_router.include_router(invites.router)`.

- [ ] **Step 4: Add last-admin protection to users.py**

In `backend/app/api/v1/users.py`, add to `assign_roles()` (before removing existing roles):

```python
    # Last-admin protection
    if target_user.id == user.id:
        raise HTTPException(status_code=400, detail="Cannot change your own roles.")
    current_roles = {ur.role.name for ur in target_user.user_roles}
    if "admin" in current_roles and "admin" not in request.role_names:
        # Check if this is the last admin
        from sqlalchemy.orm import selectinload as sil
        admin_count_result = await db.execute(
            select(func.count(UserRole.id))
            .join(Role, UserRole.role_id == Role.id)
            .where(UserRole.tenant_id == user.tenant_id, Role.name == "admin")
        )
        admin_count = admin_count_result.scalar() or 0
        if admin_count <= 1:
            raise HTTPException(status_code=400, detail="Cannot remove the last admin. Assign another admin first.")
```

Add same protection to `deactivate_user()`:

```python
    # Cannot deactivate yourself
    if target_user.id == user.id:
        raise HTTPException(status_code=400, detail="Cannot deactivate yourself.")
    # Last-admin protection
    from sqlalchemy.orm import selectinload as sil
    target_with_roles = await db.execute(
        select(User).options(selectinload(User.user_roles).selectinload(UserRole.role)).where(User.id == user_id)
    )
    target_loaded = target_with_roles.scalar_one_or_none()
    if target_loaded and any(ur.role.name == "admin" for ur in target_loaded.user_roles):
        admin_count_result = await db.execute(
            select(func.count(UserRole.id))
            .join(Role, UserRole.role_id == Role.id)
            .where(UserRole.tenant_id == user.tenant_id, Role.name == "admin")
        )
        if (admin_count_result.scalar() or 0) <= 1:
            raise HTTPException(status_code=400, detail="Cannot deactivate the last admin.")
```

- [ ] **Step 5: Run tests**

Run: `cd backend && .venv/bin/python -m pytest tests/test_invite_service.py tests/test_invites_api.py -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/v1/invites.py backend/app/api/v1/router.py backend/app/api/v1/users.py backend/tests/test_invites_api.py
git commit -m "feat: add invite API endpoints with last-admin protection"
```

---

### Task 5: Financial report permission gating in orchestrator

**Files:**
- Modify: `backend/app/services/chat/orchestrator.py:714-719`

- [ ] **Step 1: Add permission check before financial mode**

In `backend/app/services/chat/orchestrator.py`, after `is_financial = detected_intent == IntentType.FINANCIAL_REPORT` (line 719), add:

```python
                    # Gate financial reports by permission
                    if is_financial:
                        from app.core.dependencies import has_permission
                        can_access_financial = await has_permission(db, user_id, "chat.financial_reports")
                        if not can_access_financial:
                            is_financial = False
                            # Inject denial into the task so the agent responds appropriately
                            sanitized_input = (
                                sanitized_input
                                + "\n\n[SYSTEM: Financial reports are restricted for your role. "
                                "Respond to the user that financial reports require Admin or User role access. "
                                "Do NOT attempt to call netsuite_financial_report.]"
                            )
                            print("[ORCHESTRATOR] Financial report blocked — user lacks chat.financial_reports permission", flush=True)
```

- [ ] **Step 2: Write financial permission tests**

Create `backend/tests/test_financial_permission.py`:

```python
"""Tests for financial report permission gating."""

from unittest.mock import AsyncMock, patch
import pytest


class TestFinancialPermissionGating:
    @pytest.mark.asyncio
    async def test_admin_has_financial_permission(self):
        """Admin role should have chat.financial_reports permission."""
        with patch("app.core.dependencies.has_permission", new_callable=AsyncMock) as mock_hp:
            mock_hp.return_value = True
            from app.core.dependencies import has_permission
            result = await has_permission(AsyncMock(), "fake-uuid", "chat.financial_reports")
            assert result is True

    @pytest.mark.asyncio
    async def test_ops_lacks_financial_permission(self):
        """Ops role should NOT have chat.financial_reports permission."""
        with patch("app.core.dependencies.has_permission", new_callable=AsyncMock) as mock_hp:
            mock_hp.return_value = False
            from app.core.dependencies import has_permission
            result = await has_permission(AsyncMock(), "fake-uuid", "chat.financial_reports")
            assert result is False
```

- [ ] **Step 3: Commit**

```bash
git add backend/app/services/chat/orchestrator.py backend/tests/test_financial_permission.py
git commit -m "feat: gate financial reports by chat.financial_reports permission"
```

---

## Chunk 2: Frontend — Types, Hooks, Team UI, Invite Page

### Task 6: Fix frontend types + permission hook

**Files:**
- Modify: `frontend/src/lib/types.ts`
- Create: `frontend/src/hooks/use-permissions.ts`

- [ ] **Step 1: Update types**

In `frontend/src/lib/types.ts`, find the `ChatMessage` interface and add/update the `User` type nearby. The existing `Role` type needs updating. Find where Role or User types are defined and update:

```typescript
export type RoleName = "admin" | "finance" | "ops" | "readonly";

export const ROLE_DISPLAY_NAMES: Record<RoleName, string> = {
  admin: "Admin",
  finance: "User",
  ops: "Operations Only",
  readonly: "Read Only",
};
```

Also update the `User` interface in `auth-provider.tsx` or `types.ts` to include `auth_provider`:

```typescript
  auth_provider?: "email" | "google";
```

- [ ] **Step 2: Create permission hook**

Create `frontend/src/hooks/use-permissions.ts`:

```typescript
"use client";

import { useMemo, useCallback } from "react";
import { useAuth } from "@/providers/auth-provider";
import type { RoleName } from "@/lib/types";

const ROLE_PERMISSIONS: Record<RoleName, string[]> = {
  admin: [
    "tenant.manage", "users.manage", "connections.manage", "connections.view",
    "tables.view", "audit.view", "exports.csv", "exports.excel",
    "recon.run", "tools.suiteql", "schedules.manage", "approvals.manage",
    "chat.financial_reports",
  ],
  finance: [
    "connections.view", "tables.view", "audit.view", "exports.csv",
    "exports.excel", "recon.run", "tools.suiteql", "chat.financial_reports",
  ],
  ops: [
    "connections.manage", "connections.view", "tables.view", "audit.view",
    "exports.csv", "schedules.manage",
  ],
  readonly: ["connections.view", "tables.view", "audit.view"],
};

export function usePermissions() {
  const { user } = useAuth();

  const permissions = useMemo(() => {
    const roles = (user as any)?.roles as RoleName[] | undefined;
    if (!roles?.length) return new Set<string>();
    const perms = new Set<string>();
    for (const role of roles) {
      for (const p of ROLE_PERMISSIONS[role] ?? []) perms.add(p);
    }
    return perms;
  }, [(user as any)?.roles]);

  const hasPermission = useCallback(
    (codename: string) => permissions.has(codename),
    [permissions],
  );

  const isAdmin = useMemo(
    () => ((user as any)?.roles as string[] | undefined)?.includes("admin") ?? false,
    [(user as any)?.roles],
  );

  return { hasPermission, isAdmin, permissions };
}
```

- [ ] **Step 3: Verify frontend builds**

Run: `cd frontend && npm run build 2>&1 | tail -5`

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/types.ts frontend/src/hooks/use-permissions.ts
git commit -m "feat: add RoleName type, display names, and usePermissions hook"
```

---

### Task 7: Team hooks + Team section component

**Files:**
- Create: `frontend/src/hooks/use-team.ts`
- Create: `frontend/src/components/settings/team-section.tsx`
- Modify: `frontend/src/app/(dashboard)/settings/page.tsx`

- [ ] **Step 1: Create team hooks**

Create `frontend/src/hooks/use-team.ts`:

```typescript
"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";

interface TeamMember {
  id: string;
  email: string;
  full_name: string;
  is_active: boolean;
  roles: string[];
}

interface TeamInvite {
  id: string;
  email: string;
  role_name: string;
  role_display_name: string;
  status: string;
  expires_at: string;
  created_at: string;
}

export function useTeamMembers() {
  return useQuery<TeamMember[]>({
    queryKey: ["team", "members"],
    queryFn: () => apiClient.get("/api/v1/users"),
  });
}

export function useTeamInvites() {
  return useQuery<TeamInvite[]>({
    queryKey: ["team", "invites"],
    queryFn: () => apiClient.get("/api/v1/invites"),
  });
}

export function useCreateInvite() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: { email: string; role_name: string }) =>
      apiClient.post("/api/v1/invites", data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["team"] }),
  });
}

export function useRevokeInvite() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (inviteId: string) =>
      apiClient.delete(`/api/v1/invites/${inviteId}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["team"] }),
  });
}

export function useChangeUserRole() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ userId, roles }: { userId: string; roles: string[] }) =>
      apiClient.patch(`/api/v1/users/${userId}/roles`, { role_names: roles }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["team"] }),
  });
}

export function useDeactivateUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (userId: string) =>
      apiClient.delete(`/api/v1/users/${userId}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["team"] }),
  });
}
```

- [ ] **Step 2: Create TeamSection component**

Create `frontend/src/components/settings/team-section.tsx` — a self-contained component with:
- Member list with role badges, kebab menu (Change Role, Deactivate)
- Pending invitations list with revoke button
- "Invite Team Member" dialog (email + role dropdown)
- Seat count display
- "(you)" badge, last-admin disabled state

This is a large component (~300 lines). The subagent implementer should read the spec's UI layout (lines 533-574) and implement it following existing shadcn/ui patterns from the settings page.

Key imports: `useTeamMembers`, `useTeamInvites`, `useCreateInvite`, `useRevokeInvite`, `useChangeUserRole`, `useDeactivateUser` from `use-team.ts`. Icons from `lucide-react`: `Users, Mail, MoreVertical, Shield, UserX, Check, Loader2`. Use `ROLE_DISPLAY_NAMES` from types.

**Non-admin view:** Use `usePermissions()` hook. If `!hasPermission("users.manage")`, show a read-only team list (members only, no invite/role-change/deactivate actions, no pending invites section). The `GET /users` endpoint already works for all authenticated users since the list endpoint uses `require_permission("users.manage")` — for non-admin read-only, add a separate `GET /users/team` endpoint that only requires auth (no permission), or gate the UI to show the section only to admins. Simplest approach: only show the Team section to admins (hide for non-admins). Non-admins don't need to see the team list in Phase 1.

- [ ] **Step 3: Add TeamSection to settings page**

In `frontend/src/app/(dashboard)/settings/page.tsx`, import and render the TeamSection at the top of the sections list (before NetSuiteConnectionSection):

```typescript
import { TeamSection } from "@/components/settings/team-section";

// In SettingsPage render, before <NetSuiteConnectionSection />:
<TeamSection />
```

- [ ] **Step 4: Verify frontend builds**

Run: `cd frontend && npm run build 2>&1 | tail -5`

- [ ] **Step 5: Commit**

```bash
git add frontend/src/hooks/use-team.ts frontend/src/components/settings/team-section.tsx frontend/src/app/\(dashboard\)/settings/page.tsx
git commit -m "feat: add Team section to Settings with invite, role management, and seat limits"
```

---

### Task 8: Invite acceptance page

**Files:**
- Create: `frontend/src/app/invite/[token]/page.tsx`

- [ ] **Step 1: Create the invite acceptance page**

Create `frontend/src/app/invite/[token]/page.tsx` — a public page (no auth required) that:

1. Calls `GET /api/v1/invites/accept/{token}` to get invite info (tenant name, email, role, expired?)
2. Shows form: Full Name input + Password + Confirm Password
3. On submit: `POST /api/v1/invites/accept/{token}` with `{full_name, password}`
4. On success: stores tokens in localStorage, redirects to `/chat`
5. States: loading (skeleton), valid invite (form), expired, already accepted, invalid token

Key points:
- This page does NOT use the auth provider (user isn't logged in yet)
- Uses `apiClient.post()` for the accept POST — apiClient works without auth (it just won't attach a token). The `/invites/accept/{token}` endpoint is public.
- After accept, store `access_token` in localStorage and set cookie, then redirect
- Google Sign-In button is shown but disabled with "Coming Soon" tooltip (Phase 2 stub)

- [ ] **Step 2: Verify frontend builds**

Run: `cd frontend && npm run build 2>&1 | tail -5`

- [ ] **Step 3: Commit**

```bash
git add frontend/src/app/invite/\[token\]/page.tsx
git commit -m "feat: add invite acceptance page with password signup"
```

---

### Task 9: Docker rebuild + test + push

- [ ] **Step 1: Run all backend tests**

Run: `cd backend && .venv/bin/python -m pytest tests/test_invite_service.py tests/test_invites_api.py -v`

- [ ] **Step 2: Rebuild containers**

```bash
docker compose up -d --build backend
docker exec ecom-netsuite-suites-backend-1 alembic upgrade head
docker compose up -d --build --renew-anon-volumes frontend
```

- [ ] **Step 3: Manual QA**

- [ ] Settings page shows Team section with current user listed
- [ ] "Invite Team Member" button opens dialog
- [ ] Invite creates and shows in Pending Invitations
- [ ] Invite accept URL printed in backend console logs
- [ ] Visiting `/invite/{token}` shows acceptance form
- [ ] Filling form + submit creates account and redirects to dashboard
- [ ] New user appears in Team list with assigned role

- [ ] **Step 4: Push**

```bash
git push -u origin feat/user-control-panel
```
