"""Tenant Query Pattern — stores successful SuiteQL queries for cross-session learning."""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class TenantQueryPattern(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "tenant_query_patterns"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    intent_embedding = mapped_column(Vector(1536), nullable=True)
    user_question: Mapped[str] = mapped_column(Text, nullable=False)
    working_sql: Mapped[str] = mapped_column(Text, nullable=False)
    tables_used: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    columns_used: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
