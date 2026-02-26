from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class DomainKnowledgeChunk(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Curated domain knowledge chunks for JIT retrieval into agent prompts.

    System-level (not tenant-scoped) — contains expert rules, documentation,
    and patterns for SuiteQL, NetSuite schema, etc.
    """

    __tablename__ = "domain_knowledge_chunks"
    __table_args__ = (
        UniqueConstraint("source_uri", "chunk_index", name="uq_dk_source_chunk"),
        Index("ix_dk_topic_tags", "topic_tags", postgresql_using="gin"),
    )

    source_uri: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="e.g. golden_dataset/join-patterns.md",
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedding = mapped_column(Vector(1536), nullable=True)
    topic_tags: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    source_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default="expert_rules",
    )
    is_deprecated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
