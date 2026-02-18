import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ChatSession(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "chat_sessions"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    session_type: Mapped[str] = mapped_column(String(20), default="chat", server_default="chat", nullable=False)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="session",
        order_by="ChatMessage.created_at",
        lazy="selectin",
    )


class ChatMessage(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "chat_messages"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("chat_sessions.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tool_calls: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    citations: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Granular token tracking
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model_used: Mapped[str | None] = mapped_column(String(100), nullable=True)
    provider_used: Mapped[str | None] = mapped_column(String(20), nullable=True)
    is_byok: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)

    session: Mapped["ChatSession"] = relationship(back_populates="messages")


class DocChunk(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "doc_chunks"
    __table_args__ = (Index("ix_doc_chunks_tenant_source", "tenant_id", "source_path"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    source_path: Mapped[str] = mapped_column(String(512), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding = mapped_column(Vector(1024), nullable=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)
