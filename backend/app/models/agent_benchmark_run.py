"""Agent benchmark run — one row per case × side per vs-MCP benchmark run.

Each CLI invocation of `tests.agent_benchmarks.run_vs_mcp` writes N rows:
one for "ours" and (if baseline enabled) one for "mcp", per case. Rows
sharing a `run_id` came from the same invocation, so joins / groupings
by `run_id` reconstruct the full run result.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Boolean, Date, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AgentBenchmarkRun(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "agent_benchmark_runs"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    # All cases in a single CLI invocation share the same run_id.
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    run_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    case_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # "ours" | "mcp"
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    # Scores
    answer_accuracy: Mapped[float] = mapped_column(Float, nullable=False)
    tool_accuracy: Mapped[float] = mapped_column(Float, nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Cost / latency
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # For regression diffs — truncated answer preview
    answer_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Our-agent-only fields
    context_chars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    num_steps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Full tool call log (audit + debugging)
    tool_calls: Mapped[list | None] = mapped_column(JSONB, nullable=True)
