import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class ReportSeries(Base, UUIDPrimaryKeyMixin):
    """A tracking lineage for one playbook per tenant (rolling-period Stage 1).

    Reports stay immutable artifacts of one period — a report's title/narrative are
    written once at compose and never recomputed by refresh. A series instead tracks
    "the newest report for playbook X, tenant Y": each report composed in `tracking`
    mode links to it via `Report.series_id` and denormalizes the period it covers onto
    `Report.period`, so "the newest report in this series" is a cheap ordered query
    rather than a `recipe_json`/`spec_json` parse. The dashboard wall follows the
    series, not a mutated report.

    UNIQUE (tenant_id, playbook_key): one tracking series per playbook per tenant.

    Deleting a series does NOT delete its reports — `Report.series_id`'s FK is
    `ON DELETE SET NULL`, so a report that was being tracked simply stops being tracked
    and remains a valid standalone artifact. That is entirely the FK's job; this model
    deliberately has no `relationship()` back to `Report` that could produce a
    different (e.g. cascading) outcome.
    """

    __tablename__ = "report_series"
    __table_args__ = (UniqueConstraint("tenant_id", "playbook_key", name="uq_report_series_tenant_playbook"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    playbook_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
