"""AgentLabRun — one row per benchmark or experiment run triggered from the agent-lab UI.

Case-level results persist in `agent_benchmark_runs` or `experiment_log` (existing
tables). This table tracks the run envelope: who triggered it, when it started/ended,
how far it got, total cost. The `agent_lab_runs_single_running` partial unique index
enforces one-running-at-a-time per (tenant, kind).
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AgentLabRun(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "agent_lab_runs"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    triggered_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # benchmark | experiment
    mode: Mapped[str] = mapped_column(String(10), nullable=False)  # all | single
    case_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="running"
    )
    total_cases: Mapped[int] = mapped_column(Integer, nullable=False)
    cases_completed: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    cost_usd_actual: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="0.0"
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index(
            "agent_lab_runs_tenant_kind_started_idx",
            "tenant_id",
            "kind",
            "started_at",
        ),
        Index(
            "agent_lab_runs_single_running",
            "tenant_id",
            "kind",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
    )
