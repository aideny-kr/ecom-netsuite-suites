from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TenantLearnedRule(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    Semantic Memory for a Tenant.
    Stores explicit business logic, rules, or corrections dictated by the user.
    These rules are mapped and injected into the AI's system prompt context
    so it becomes hyper-aware of custom semantics over time.
    """

    __tablename__ = "tenant_learned_rules"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    
    # Optional categorization (e.g., "query_logic", "term_definition")
    rule_category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    
    # The actual business logic learned (e.g., "'failed' means status=3 AND has_error=T")
    rule_description: Mapped[str] = mapped_column(Text, nullable=False)
    
    # Whether this rule is currently active and should be injected
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Who created this rule (admin audit trail)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )

    # Relationships
    tenant: Mapped["Tenant"] = relationship("Tenant", foreign_keys=[tenant_id])
