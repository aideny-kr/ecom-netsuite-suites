"""Agent benchmark runs — tracks vs-MCP benchmark results over time.

Each row is a single case × side (ours or mcp) for a benchmark run,
so we can compute deltas and detect regressions day-over-day.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "066_bench_vs_mcp"
down_revision = "065_fiscal_year_start"


def upgrade() -> None:
    op.create_table(
        "agent_benchmark_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False, index=True),
        # All cases in a single CLI invocation share the same run_id so we can
        # group them ("the 2026-04-09 10:00 UTC run included cases X, Y, Z").
        sa.Column("run_id", UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("run_date", sa.Date(), nullable=False, index=True),
        sa.Column("case_id", sa.String(128), nullable=False),
        # "ours" | "mcp"
        sa.Column("side", sa.String(16), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        # Scores
        sa.Column("answer_accuracy", sa.Float(), nullable=False),
        sa.Column("tool_accuracy", sa.Float(), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        # Cost / latency
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        # For regression diffs: preview of the final answer text (truncated)
        sa.Column("answer_preview", sa.Text(), nullable=True),
        # Our-agent-only: context size + step count + confidence
        sa.Column("context_chars", sa.Integer(), nullable=True),
        sa.Column("num_steps", sa.Integer(), nullable=True),
        sa.Column("confidence_score", sa.Float(), nullable=True),
        # Full tool call log as JSONB — audit + debugging
        sa.Column("tool_calls", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # Index for the most common query: "latest run per tenant per case"
    op.create_index(
        "ix_agent_benchmark_runs_tenant_case_date",
        "agent_benchmark_runs",
        ["tenant_id", "case_id", "run_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_benchmark_runs_tenant_case_date", table_name="agent_benchmark_runs")
    op.drop_table("agent_benchmark_runs")
