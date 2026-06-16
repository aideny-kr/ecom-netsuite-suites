# Publishable Report Renderer — Slice 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user generate, from chat, a branded story-telling financial report from already-computed backend data, save it as a frozen self-contained HTML artifact, and click it to render in the browser.

**Architecture:** Browserless, frozen-snapshot. `report.compose` (an MCP tool, repurposing the reserved `report.export` stub) resolves **full** frozen payloads from the assistant message's `tool_calls[].result_payload` (NOT the 50-row-capped Redis result cache), fills templated-narrative placeholders, renders charts to **server-side SVG**, assembles `spec_json`, and renders one self-contained HTML document into a new RLS-scoped `reports` row. The frontend serves that saved HTML in a blob-URL `<iframe>` at `/reports/[id]`. No headless browser, no new container.

**Tech Stack:** FastAPI + SQLAlchemy 2.0 (`Mapped`/`mapped_column`) + Alembic + Postgres RLS; Next.js 14 App Router (`"use client"`) + React Query + Tailwind; Anthropic unified-agent MCP tool seam.

**Spec:** `docs/superpowers/specs/2026-06-09-publishable-report-renderer-design.md`
**Tier:** **T2** (new RLS table + Alembic migration + new chat prompt surface). Gates in Task 15.

---

## File Structure

**Backend (new):**
- `backend/alembic/versions/084_reports.py` — `reports` table + RLS + FORCE RLS. (`083` is intentionally unused — the dropped merge migration; alembic revision ids are arbitrary strings, so `084` chaining off `082_metric_def_with_check` is valid.)
- `backend/app/models/report.py` — `Report` ORM model.
- `backend/app/schemas/report.py` — `ComposeSection` union + `ReportResponse`.
- `backend/app/services/report/report_charts.py` — server-side neubrutalist SVG renderer (bar/line/pie/area).
- `backend/app/services/report/report_html.py` — `render_report_html(spec_json) -> str` (template + neubrutalism CSS).
- `backend/app/services/report/report_service.py` — `compose_report(...)` orchestration.
- `backend/app/api/v1/reports.py` — list / get / view endpoints.
- `backend/app/services/chat/knowledge_profiles/reporting.yaml` — compose prompt fragment.

**Backend (modify):** `backend/app/mcp/tools/report_export.py` (repurpose in place), `backend/app/mcp/registry.py`, `backend/app/mcp/governance.py`, `backend/app/services/chat/nodes.py`, `backend/app/services/chat/tool_categories.py`, `backend/app/services/chat/orchestrator.py` (interception branch), `backend/app/services/chat/prompts.py`, `backend/app/api/v1/router.py`, `backend/tests/test_prompt_tool_sync.py` + 6 test fixtures (rename).

**Frontend (new):** `frontend/src/components/chat/report-ready-card.tsx`, `frontend/src/app/(dashboard)/reports/page.tsx`, `frontend/src/app/(dashboard)/reports/[id]/page.tsx`, `frontend/src/hooks/use-reports.ts`.

**Frontend (modify):** `frontend/src/lib/chat-stream.ts`, `frontend/src/components/chat/message-list.tsx`, `frontend/src/app/(dashboard)/chat/page.tsx`, `frontend/src/lib/constants.ts` (NAV), `frontend/src/app/globals.css` (neubrutalism tokens for the app-chrome wrapper).

**Ownership note (for parallel execution):** Tasks 1–8 are backend-only; Tasks 9–12 are frontend-only. They touch disjoint file sets and may run in separate worktrees per CLAUDE.md. Tasks 13–15 are integration/gates and run last, sequentially.

**Decided design points (resolving spec §16 open questions):**
- **§16.1 full rows:** resolve from `ChatMessage.tool_calls[].result_payload` (uncapped, built by `extract_result_payload`), NOT the Redis result cache (`CachedResult.to_json()` caps `rows[:50]`).
- **§16.2 iframe auth:** the view page does `apiClient.get` the HTML → `URL.createObjectURL(new Blob([html],{type:'text/html'}))` → iframe `src`. No cross-origin iframe GET (which would carry no bearer and 401).
- **§16.4 chart scope (YAGNI):** v1 renders `bar`/`line`/`pie`/`area`; `scatter`/`donut`/`histogram` render an explicit "chart type not yet supported" placeholder (never crash).

---

## Task 1: `reports` table — migration 084 + model + RLS

**Files:**
- Create: `backend/alembic/versions/084_reports.py`
- Create: `backend/app/models/report.py`
- Test: `backend/tests/test_report_migration.py`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_report_migration.py
from sqlalchemy import select, text
from app.core.database import set_tenant_context
from app.models.report import Report
from tests.conftest import create_test_tenant  # pattern: test_saved_queries.py


async def test_reports_table_columns_exist(db):
    cols = (
        await db.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name='reports'")
        )
    ).scalars().all()
    assert {
        "id", "tenant_id", "title", "spec_json", "rendered_html",
        "status", "source_run_id", "created_by", "version",
        "published_drive_url", "published_at", "created_at", "updated_at",
    } <= set(cols)


async def test_reports_rls_blocks_cross_tenant(db):
    """Under FORCE RLS, a report written as tenant A is invisible under tenant B's context.
    Mirrors test_metric_catalog_seeder.py:127 — the REAL policy test (no ORM .where filter)."""
    tenant_a = await create_test_tenant(db, name="Corp A")
    tenant_b = await create_test_tenant(db, name="Corp B")

    await set_tenant_context(db, str(tenant_a.id))
    db.add(Report(
        tenant_id=tenant_a.id, title="A report", spec_json={"sections": []},
        rendered_html="<html></html>", created_by=None,
    ))
    await db.flush()

    await set_tenant_context(db, str(tenant_b.id))
    rows = (await db.execute(select(Report))).scalars().all()  # NO .where — RLS must hide it
    assert rows == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_report_migration.py -v`
Expected: FAIL (no `reports` table / no `app.models.report`).

> **NOTE on the RLS test (extraction gotcha):** the local `db` fixture may run as a superuser/owner that bypasses RLS even with FORCE — so a green local result does NOT prove the policy. The authoritative proof is the **post-deploy live smoke** (Task 15) against `uat-smoke`, exactly as the `082` WITH-CHECK smoke was needed. If the local test passes vacuously, keep it (it guards the ORM/model) and rely on the live smoke for the policy.

- [ ] **Step 3: Write the model**

```python
# backend/app/models/report.py  (template: backend/app/models/metric_definition.py)
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    spec_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    rendered_html: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft", server_default="draft")
    source_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    published_drive_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
```

Register the model import where models are aggregated (grep `from app.models.metric_definition import` to find the `app/models/__init__.py` or metadata import site; add `report` alongside).

- [ ] **Step 4: Write the migration** (template: `080_metric_definitions.py` create + `082` policy + `081` FORCE)

```python
# backend/alembic/versions/084_reports.py
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "084_reports"
down_revision = "082_metric_def_with_check"  # current single head (verify: alembic heads)
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reports",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("spec_json", JSONB(), nullable=False),
        sa.Column("rendered_html", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("source_run_id", UUID(as_uuid=True), nullable=True),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("published_drive_url", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_reports_tenant", "reports", ["tenant_id"])
    # RLS — NO OR-SYSTEM branch (reports are never SYSTEM-owned).
    op.execute("ALTER TABLE reports ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY reports_tenant_isolation ON reports
        USING (tenant_id = get_current_tenant_id())
        WITH CHECK (tenant_id = get_current_tenant_id())
    """)
    op.execute("ALTER TABLE reports FORCE ROW LEVEL SECURITY")  # load-bearing on Supabase (owner != BYPASSRLS)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS reports_tenant_isolation ON reports")
    op.drop_table("reports")
```

- [ ] **Step 5: Apply migration to BOTH DBs and run tests**

```bash
cd backend && .venv/bin/alembic upgrade head          # Supabase (verify: .venv/bin/alembic heads shows ONE head)
docker exec ecom-netsuite-suites-backend-1 alembic upgrade head   # local docker
.venv/bin/python -m pytest tests/test_report_migration.py -v
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/alembic/versions/084_reports.py backend/app/models/report.py backend/tests/test_report_migration.py
git commit -m "feat(reports): reports table + RLS (FORCE) — migration 084"
```

---

## Task 2: Report schemas (`ComposeSection` union + `ReportResponse`)

**Files:**
- Create: `backend/app/schemas/report.py`
- Test: `backend/tests/test_report_schemas.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_report_schemas.py
import pytest
from pydantic import ValidationError
from app.schemas.report import ComposeRequest, parse_sections


def test_parse_valid_sections():
    req = ComposeRequest(title="Q2", sections=[
        {"type": "heading", "level": 1, "text": "Q2 Review"},
        {"type": "narrative", "markdown": "Revenue grew {{result:r1.total}}."},
        {"type": "metric_headline", "result_id": "m1", "label": "Revenue"},
        {"type": "chart", "result_id": "r1", "chart_type": "bar"},
        {"type": "table", "result_id": "r1"},
        {"type": "divider"},
    ])
    secs = parse_sections(req.sections)
    assert [s.type for s in secs] == ["heading", "narrative", "metric_headline", "chart", "table", "divider"]


def test_reject_unknown_section_type():
    with pytest.raises(ValidationError):
        ComposeRequest(title="x", sections=[{"type": "bogus"}])
```

- [ ] **Step 2: Run to verify it fails**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_report_schemas.py -v`
Expected: FAIL (`app.schemas.report` missing).

- [ ] **Step 3: Implement**

```python
# backend/app/schemas/report.py
from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter


class HeadingSection(BaseModel):
    type: Literal["heading"]
    level: int = Field(default=2, ge=1, le=3)
    text: str


class NarrativeSection(BaseModel):
    type: Literal["narrative"]
    markdown: str  # may contain {{result:<id>.<field>}} / {{metric:<id>}} placeholders


class MetricHeadlineSection(BaseModel):
    type: Literal["metric_headline"]
    result_id: str
    label: str | None = None


class ChartSection(BaseModel):
    type: Literal["chart"]
    result_id: str
    chart_type: Literal["bar", "line", "pie", "area", "scatter", "donut", "histogram"] | None = None


class TableSection(BaseModel):
    type: Literal["table"]
    result_id: str
    select: list[str] | None = None


class DividerSection(BaseModel):
    type: Literal["divider"]


ComposeSection = Annotated[
    Union[HeadingSection, NarrativeSection, MetricHeadlineSection, ChartSection, TableSection, DividerSection],
    Field(discriminator="type"),
]

_SECTIONS_ADAPTER = TypeAdapter(list[ComposeSection])


def parse_sections(raw: list[dict]) -> list:
    return _SECTIONS_ADAPTER.validate_python(raw)


class ComposeRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    sections: list[dict] = Field(min_length=1)


class ReportResponse(BaseModel):
    id: str
    title: str
    status: str
    version: int
    created_at: datetime
    model_config = {"from_attributes": True}
```

- [ ] **Step 4: Run to verify it passes**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_report_schemas.py -v` → PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/schemas/report.py backend/tests/test_report_schemas.py
git commit -m "feat(reports): compose-section union + response schemas"
```

---

## Task 3: Neubrutalist SVG chart renderer (bar/line/pie/area)

**Files:**
- Create: `backend/app/services/report/__init__.py` (empty), `backend/app/services/report/report_charts.py`
- Test: `backend/tests/test_report_charts.py`

The input is the existing `ChartData` (`backend/app/schemas/chart.py`): `chart_type`, `title`, `x_axis: ChartAxis{label,key,color}`, `y_axes: list[ChartAxis]`, `data: list[dict]`, `options`. Output is a self-contained `<svg>` string (fixed `width=720 height=380`, inline styles).

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_report_charts.py
from app.schemas.chart import ChartAxis, ChartData
from app.services.report.report_charts import render_chart_svg

def _bar():
    return ChartData(chart_type="bar", title="Rev", x_axis=ChartAxis(label="P", key="period"),
        y_axes=[ChartAxis(label="Revenue", key="revenue", color="#6366f1")],
        data=[{"period": "Q1", "revenue": 100}, {"period": "Q2", "revenue": 150}])

def test_bar_renders_svg():
    svg = render_chart_svg(_bar())
    assert svg.startswith("<svg") and "</svg>" in svg
    assert "Q1" in svg and "Q2" in svg          # x labels present
    assert "<rect" in svg                         # bars drawn

def test_deterministic():
    assert render_chart_svg(_bar()) == render_chart_svg(_bar())

def test_unsupported_type_is_placeholder_not_crash():
    c = _bar(); c.chart_type = "histogram"
    svg = render_chart_svg(c)
    assert "<svg" in svg and "not yet supported" in svg.lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_report_charts.py -v` → FAIL

- [ ] **Step 3: Implement** (hand-rolled SVG; neubrutalist = thick black strokes, flat fills, hard offset shadow via an offset duplicate, no gradients/animation)

```python
# backend/app/services/report/report_charts.py
from __future__ import annotations

from html import escape

from app.schemas.chart import ChartData

_W, _H = 720, 380
_PAD_L, _PAD_B, _PAD_T, _PAD_R = 64, 56, 48, 24
_PALETTE = ["#6366f1", "#ef4444", "#f59e0b", "#10b981", "#0ea5e9", "#a855f7"]


def _fmt(v: float) -> str:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(n) >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if abs(n) >= 1_000:
        return f"{n/1_000:.1f}K"
    return f"{n:.0f}"


def _frame(body: str, title: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_W}" height="{_H}" viewBox="0 0 {_W} {_H}" '
        f'font-family="\'Inter\',system-ui,sans-serif">'
        f'<rect x="2" y="2" width="{_W-4}" height="{_H-4}" fill="#FFFFFF" stroke="#000" stroke-width="3"/>'
        f'<text x="20" y="30" font-size="18" font-weight="800" fill="#111">{escape(title)}</text>'
        f"{body}</svg>"
    )


def _num(row: dict, key: str) -> float:
    try:
        return float(row.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _bars(c: ChartData) -> str:
    rows, series = c.data, c.y_axes
    if not rows or not series:
        return ""
    plot_w = _W - _PAD_L - _PAD_R
    plot_h = _H - _PAD_T - _PAD_B
    y0 = _PAD_T + plot_h
    vmax = max((_num(r, s.key) for r in rows for s in series), default=0) or 1
    group_w = plot_w / len(rows)
    bar_w = group_w / (len(series) + 1)
    out = [f'<line x1="{_PAD_L}" y1="{y0}" x2="{_W-_PAD_R}" y2="{y0}" stroke="#000" stroke-width="2"/>']
    for i, row in enumerate(rows):
        gx = _PAD_L + i * group_w
        for j, s in enumerate(series):
            h = (_num(row, s.key) / vmax) * plot_h
            x = gx + bar_w * (j + 0.5)
            y = y0 - h
            color = s.color or _PALETTE[j % len(_PALETTE)]
            # hard offset shadow (no blur) then the bar
            out.append(f'<rect x="{x+4:.1f}" y="{y+4:.1f}" width="{bar_w:.1f}" height="{h:.1f}" fill="#000"/>')
            out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" '
                       f'fill="{color}" stroke="#000" stroke-width="2"/>')
        label = escape(str(row.get(c.x_axis.key, "")))
        out.append(f'<text x="{gx+group_w/2:.1f}" y="{y0+20}" font-size="12" font-weight="600" '
                   f'text-anchor="middle" fill="#111">{label}</text>')
    out.append(f'<text x="{_PAD_L-8}" y="{_PAD_T+8}" font-size="11" text-anchor="end" fill="#444">{_fmt(vmax)}</text>')
    return "".join(out)


def _lines(c: ChartData, area: bool) -> str:
    rows, series = c.data, c.y_axes
    if not rows or not series:
        return ""
    plot_w = _W - _PAD_L - _PAD_R
    plot_h = _H - _PAD_T - _PAD_B
    y0 = _PAD_T + plot_h
    vmax = max((_num(r, s.key) for r in rows for s in series), default=0) or 1
    step = plot_w / max(len(rows) - 1, 1)
    out = [f'<line x1="{_PAD_L}" y1="{y0}" x2="{_W-_PAD_R}" y2="{y0}" stroke="#000" stroke-width="2"/>']
    for j, s in enumerate(series):
        color = s.color or _PALETTE[j % len(_PALETTE)]
        pts = [(_PAD_L + i * step, y0 - (_num(r, s.key) / vmax) * plot_h) for i, r in enumerate(rows)]
        path = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        if area:
            poly = f"{_PAD_L},{y0} " + path + f" {_PAD_L + (len(rows)-1)*step:.1f},{y0}"
            out.append(f'<polygon points="{poly}" fill="{color}" fill-opacity="0.25"/>')
        out.append(f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="3"/>')
        for x, y in pts:
            out.append(f'<rect x="{x-4:.1f}" y="{y-4:.1f}" width="8" height="8" fill="{color}" stroke="#000" stroke-width="2"/>')
    for i, r in enumerate(rows):
        out.append(f'<text x="{_PAD_L+i*step:.1f}" y="{y0+20}" font-size="12" font-weight="600" '
                   f'text-anchor="middle" fill="#111">{escape(str(r.get(c.x_axis.key,"")))}</text>')
    return "".join(out)


def _pie(c: ChartData) -> str:
    import math
    rows = c.data
    key = c.y_axes[0].key if c.y_axes else None
    if not rows or not key:
        return ""
    total = sum(_num(r, key) for r in rows) or 1
    cx, cy, rad = _W / 2, _H / 2 + 10, 130
    out, ang = [], -math.pi / 2
    for i, r in enumerate(rows):
        frac = _num(r, key) / total
        a2 = ang + frac * 2 * math.pi
        large = 1 if frac > 0.5 else 0
        x1, y1 = cx + rad * math.cos(ang), cy + rad * math.sin(ang)
        x2, y2 = cx + rad * math.cos(a2), cy + rad * math.sin(a2)
        out.append(f'<path d="M{cx},{cy} L{x1:.1f},{y1:.1f} A{rad},{rad} 0 {large} 1 {x2:.1f},{y2:.1f} Z" '
                   f'fill="{_PALETTE[i % len(_PALETTE)]}" stroke="#000" stroke-width="2"/>')
        ang = a2
    return "".join(out)


def render_chart_svg(chart: ChartData) -> str:
    t = chart.chart_type
    if t == "bar":
        return _frame(_bars(chart), chart.title)
    if t == "line":
        return _frame(_lines(chart, area=False), chart.title)
    if t == "area":
        return _frame(_lines(chart, area=True), chart.title)
    if t == "pie":
        return _frame(_pie(chart), chart.title)
    placeholder = (f'<text x="{_W/2}" y="{_H/2}" font-size="14" text-anchor="middle" fill="#666">'
                   f'Chart type "{escape(t)}" not yet supported</text>')
    return _frame(placeholder, chart.title)
```

- [ ] **Step 4: Run to verify it passes**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_report_charts.py -v` → PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/report/ backend/tests/test_report_charts.py
git commit -m "feat(reports): server-side neubrutalist SVG chart renderer (bar/line/pie/area)"
```

---

## Task 4: `render_report_html(spec_json) -> str`

**Files:**
- Create: `backend/app/services/report/report_html.py`
- Test: `backend/tests/test_report_html.py`

Renders the canonical `spec_json` (frozen, post-resolution — every data section already carries its values + each chart section carries its `svg`) into ONE self-contained HTML document with inline neubrutalism CSS. This same string is served in the iframe AND (Slice 2) fed to weasyprint.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_report_html.py
from app.services.report.report_html import render_report_html

def test_render_self_contained_html():
    spec = {"title": "Q2 Review", "generated_at": "2026-06-10T00:00:00Z", "sections": [
        {"type": "heading", "level": 1, "text": "Q2 Review"},
        {"type": "narrative", "markdown": "Revenue grew **12%** this quarter."},
        {"type": "metric_headline", "label": "Revenue", "value": "1.2M", "unit": "USD", "period": "Q2", "definition_version": 3},
        {"type": "chart", "svg": "<svg id='c1'></svg>"},
        {"type": "table", "columns": ["Period", "Revenue"], "rows": [["Q1", "100"], ["Q2", "150"]], "row_count": 2},
        {"type": "divider"},
    ], "provenance": {"sources": ["metric:revenue@v3"]}}
    html = render_report_html(spec, accent_hsl="142 70% 45%")
    assert html.lstrip().startswith("<!DOCTYPE html>")
    assert "<style>" in html                      # inline CSS, self-contained
    assert "Q2 Review" in html
    assert "<svg id='c1'></svg>" in html          # chart svg embedded verbatim
    assert "150" in html                           # table value
    assert "definition" in html.lower()            # provenance footnote rendered

def test_html_escapes_user_text():
    spec = {"title": "<script>x</script>", "sections": [], "provenance": {}}
    html = render_report_html(spec, accent_hsl="0 0% 0%")
    assert "<script>x</script>" not in html        # escaped
```

- [ ] **Step 2: Run to verify it fails** → FAIL

- [ ] **Step 3: Implement**

```python
# backend/app/services/report/report_html.py
from __future__ import annotations

from html import escape

_CSS = """
:root { --bg:#FAF9F6; --ink:#111; --border:#000; --card:#FFF; --accent:hsl(%(accent)s); }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font-family:'Inter',system-ui,-apple-system,sans-serif; line-height:1.5; }
.report { max-width:840px; margin:0 auto; padding:48px 32px; }
h1,h2,h3 { font-weight:800; letter-spacing:-0.02em; margin:1.4em 0 0.4em; }
h1 { font-size:38px; } h2 { font-size:26px; } h3 { font-size:20px; }
.nb-card { background:var(--card); border:3px solid var(--border); box-shadow:6px 6px 0 var(--border);
  padding:24px; margin:24px 0; }
.metric { display:flex; flex-direction:column; gap:4px; }
.metric .value { font-size:44px; font-weight:800; }
.metric .label { font-size:14px; font-weight:700; text-transform:uppercase; letter-spacing:0.04em; }
.metric .foot { font-size:12px; color:#666; }
.accent-bar { height:10px; background:var(--accent); border:3px solid var(--border); margin:0 0 24px; }
table { width:100%; border-collapse:collapse; }
th,td { border:2px solid var(--border); padding:8px 12px; text-align:left; font-size:14px; }
th { background:var(--accent); font-weight:800; }
.divider { height:0; border-top:3px solid var(--border); margin:32px 0; }
.svg-wrap { overflow:auto; }
.prov { font-size:12px; color:#666; border-top:2px dashed #999; margin-top:48px; padding-top:12px; }
"""


def _md_inline(text: str) -> str:
    # Minimal: escape, then **bold**. (No raw HTML passthrough — trust boundary + XSS safety.)
    import re
    esc = escape(text)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", esc)


def _section_html(s: dict) -> str:
    t = s.get("type")
    if t == "heading":
        lvl = min(max(int(s.get("level", 2)), 1), 3)
        return f"<h{lvl}>{escape(str(s.get('text','')))}</h{lvl}>"
    if t == "narrative":
        return f'<div class="nb-card">{_md_inline(str(s.get("markdown","")))}</div>'
    if t == "metric_headline":
        foot = ""
        if s.get("definition_version") is not None:
            foot = f'<span class="foot">definition v{escape(str(s["definition_version"]))} · {escape(str(s.get("period","")))}</span>'
        return (f'<div class="nb-card metric"><span class="label">{escape(str(s.get("label","")))}</span>'
                f'<span class="value">{escape(str(s.get("value","")))} '
                f'<small>{escape(str(s.get("unit","")))}</small></span>{foot}</div>')
    if t == "chart":
        return f'<div class="nb-card svg-wrap">{s.get("svg","")}</div>'  # svg is server-generated, trusted
    if t == "table":
        cols = "".join(f"<th>{escape(str(c))}</th>" for c in s.get("columns", []))
        body = "".join(
            "<tr>" + "".join(f"<td>{escape(str(v))}</td>" for v in row) + "</tr>"
            for row in s.get("rows", [])
        )
        note = "" if not s.get("truncated") else f'<p class="foot">Showing first rows of {escape(str(s.get("row_count","")))}.</p>'
        return f'<div class="nb-card svg-wrap"><table><thead><tr>{cols}</tr></thead><tbody>{body}</tbody></table>{note}</div>'
    if t == "divider":
        return '<div class="divider"></div>'
    if t == "error":
        return f'<div class="nb-card" style="border-color:#ef4444"><strong>Data unavailable:</strong> {escape(str(s.get("reason","")))}</div>'
    return ""


def render_report_html(spec: dict, accent_hsl: str = "240 6% 10%") -> str:
    title = escape(str(spec.get("title", "Report")))
    body = "".join(_section_html(s) for s in spec.get("sections", []))
    prov = spec.get("provenance", {}) or {}
    sources = prov.get("sources", [])
    prov_html = ""
    if sources:
        items = "".join(f"<li>{escape(str(x))}</li>" for x in sources)
        prov_html = f'<div class="prov"><strong>Sources &amp; definitions</strong><ul>{items}</ul></div>'
    css = _CSS % {"accent": escape(accent_hsl)}
    return (
        f"<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{title}</title><style>{css}</style></head><body><div class=\"report\">"
        f'<div class="accent-bar"></div><h1>{title}</h1>{body}{prov_html}</div></body></html>"
    )
```

- [ ] **Step 4: Run to verify it passes** → PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/report/report_html.py backend/tests/test_report_html.py
git commit -m "feat(reports): self-contained neubrutalist HTML renderer"
```

---

## Task 5: `report_service.compose_report` (resolve full payloads → freeze → render → persist)

**Files:**
- Create: `backend/app/services/report/report_service.py`
- Test: `backend/tests/test_report_service.py`

**Key:** resolve each `result_id` to the FULL frozen payload from `ChatMessage.tool_calls[].result_payload` (uncapped — built by `extract_result_payload`), NOT the 50-row Redis cache. Fill `{{result:<id>.<field>}}` / `{{metric:<id>}}` placeholders deterministically. The TOOL path must `set_tenant_context` before INSERT (it runs outside the request dependency chain).

- [ ] **Step 1: Write the failing tests** (resolver is injected so the test supplies frozen payloads directly)

```python
# backend/tests/test_report_service.py
import pytest
from app.services.report.report_service import compose_report, fill_placeholders

FROZEN = {
    "r1": {"columns": ["Period", "Revenue"], "rows": [["Q1", "100"], ["Q2", "150"]], "row_count": 2},
    "m1": {"value": "1.2M", "unit": "USD", "period": "Q2", "definition_version": 3, "columns": [], "rows": []},
}

def _resolver(rid):
    if rid not in FROZEN:
        raise KeyError(rid)
    return FROZEN[rid]


def test_fill_placeholders_injects_frozen_values():
    out = fill_placeholders("Revenue is {{result:m1.value}} for {{metric:m1}}", _resolver)
    assert "1.2M" in out and "{{" not in out

def test_fill_placeholders_unresolved_is_marked_not_fabricated():
    out = fill_placeholders("x {{result:nope.value}}", _resolver)
    assert "[unresolved: result:nope.value]" in out

@pytest.mark.anyio
async def test_compose_assembles_frozen_spec(monkeypatch):
    sections = [
        {"type": "narrative", "markdown": "Rev grew to {{result:m1.value}}."},
        {"type": "table", "result_id": "r1"},
        {"type": "chart", "result_id": "r1", "chart_type": "bar"},
        {"type": "metric_headline", "result_id": "m1", "label": "Revenue"},
    ]
    spec, html, condensed = await compose_report.__wrapped__(  # pure assembly, db/persist stubbed below
        title="Q2", sections=sections, resolver=_resolver, accent_hsl="0 0% 0%",
    )
    # narrative figure injected by backend, not the LLM
    narr = next(s for s in spec["sections"] if s["type"] == "narrative")
    assert "1.2M" in narr["markdown"]
    # table carries FULL frozen rows
    tbl = next(s for s in spec["sections"] if s["type"] == "table")
    assert tbl["rows"] == [["Q1", "100"], ["Q2", "150"]]
    # chart pre-rendered to svg
    chart = next(s for s in spec["sections"] if s["type"] == "chart")
    assert chart["svg"].startswith("<svg")
    # trust boundary: condensed LLM payload has NO computed numbers
    assert "1.2M" not in condensed and "150" not in condensed
    assert "report_id" in condensed or "section_count" in condensed
```

> The test calls a pure-assembly inner function. Structure `report_service` so spec/HTML assembly is separable from DB persistence (inject `resolver`); the DB write path is exercised in the Task 13 e2e.

- [ ] **Step 2: Run to verify it fails** → FAIL

- [ ] **Step 3: Implement**

```python
# backend/app/services/report/report_service.py
from __future__ import annotations

import re
import uuid
from typing import Any, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import set_tenant_context
from app.schemas.chart import ChartData
from app.schemas.report import parse_sections
from app.services.report.report_charts import render_chart_svg
from app.services.report.report_html import render_report_html

_PLACEHOLDER = re.compile(r"\{\{(result|metric):([^}]+)\}\}")
Resolver = Callable[[str], dict]


def fill_placeholders(text: str, resolver: Resolver) -> str:
    def _sub(m: re.Match) -> str:
        kind, ref = m.group(1), m.group(2).strip()
        rid, _, field = ref.partition(".")
        try:
            payload = resolver(rid)
        except Exception:
            return f"[unresolved: {kind}:{ref}]"
        if kind == "metric":
            field = field or "value"
        val = payload.get(field) if field else payload.get("value")
        return str(val) if val is not None else f"[unresolved: {kind}:{ref}]"
    return _PLACEHOLDER.sub(_sub, text)


def _resolve_data_section(s: dict, resolver: Resolver) -> dict:
    try:
        payload = resolver(s["result_id"])
    except Exception as exc:
        return {"type": "error", "reason": f"{s.get('result_id')}: {exc}"}
    if s["type"] == "table":
        cols, rows = payload.get("columns", []), payload.get("rows", [])
        if s.get("select"):
            idx = [cols.index(c) for c in s["select"] if c in cols]
            cols = [cols[i] for i in idx]
            rows = [[r[i] for i in idx] for r in rows]
        return {"type": "table", "columns": cols, "rows": rows,
                "row_count": payload.get("row_count", len(rows)), "truncated": payload.get("truncated", False)}
    if s["type"] == "metric_headline":
        return {"type": "metric_headline", "label": s.get("label") or payload.get("display_name", ""),
                "value": payload.get("value", ""), "unit": payload.get("unit", ""),
                "period": payload.get("period", ""), "definition_version": payload.get("definition_version")}
    if s["type"] == "chart":
        cd = payload.get("chart_data")
        if cd is None:  # build a minimal ChartData from a tabular payload
            cols = payload.get("columns", [])
            chart = ChartData(chart_type=s.get("chart_type") or "bar", title=s.get("label") or "Chart",
                x_axis={"label": cols[0] if cols else "x", "key": cols[0] if cols else "x"},
                y_axes=[{"label": c, "key": c} for c in cols[1:]] or [{"label": "value", "key": "value"}],
                data=[dict(zip(cols, r)) for r in payload.get("rows", [])])
        else:
            chart = ChartData.model_validate(cd)
            if s.get("chart_type"):
                chart.chart_type = s["chart_type"]
        return {"type": "chart", "svg": render_chart_svg(chart), "chart_type": chart.chart_type}
    return s


def assemble_spec(title: str, sections: list[dict], resolver: Resolver) -> dict:
    parse_sections(sections)  # validates shape; raises on unknown type
    provenance_sources: list[str] = []
    out_sections: list[dict] = []
    for s in sections:
        t = s["type"]
        if t == "narrative":
            out_sections.append({"type": "narrative", "markdown": fill_placeholders(s["markdown"], resolver)})
        elif t in ("table", "metric_headline", "chart"):
            resolved = _resolve_data_section(s, resolver)
            out_sections.append(resolved)
            if resolved.get("type") == "metric_headline" and resolved.get("definition_version") is not None:
                provenance_sources.append(f"metric:{s['result_id']}@v{resolved['definition_version']}")
        else:  # heading / divider
            out_sections.append(s)
    return {"title": title, "sections": out_sections, "provenance": {"sources": provenance_sources}}


async def compose_report(
    db: AsyncSession, *, tenant_id, title: str, sections: list[dict], resolver: Resolver,
    created_by=None, source_run_id=None, accent_hsl: str = "240 6% 10%",
) -> dict:
    from app.models.report import Report
    spec = assemble_spec(title, sections, resolver)
    html = render_report_html(spec, accent_hsl=accent_hsl)
    await set_tenant_context(db, str(tenant_id))  # TOOL path: RLS context not pre-set
    report = Report(tenant_id=tenant_id, title=title, spec_json=spec, rendered_html=html,
                    created_by=created_by, source_run_id=source_run_id)
    db.add(report)
    await db.flush()
    await db.commit()
    return {"report_id": str(report.id), "title": title, "section_count": len(spec["sections"])}
```

> The Task 1–4 test for `compose_report.__wrapped__` expects a pure `(spec, html, condensed)` tuple. Refactor: keep `assemble_spec` + `render_report_html` pure (tested directly), and have the tool layer build `condensed`. Adjust the test to call `assemble_spec` + `render_report_html` directly rather than `__wrapped__` if cleaner — the intent is: assembly is pure and unit-tested, persistence is e2e-tested.

- [ ] **Step 4: Run to verify it passes** → PASS (adjust test per the note)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/report/report_service.py backend/tests/test_report_service.py
git commit -m "feat(reports): compose_report — resolve full frozen payloads, fill narrative, render, persist"
```

---

## Task 6: Repurpose `report.export` → `report.compose` tool (wiring + traps)

**Files (modify):** `backend/app/mcp/tools/report_export.py`, `registry.py`, `governance.py`, `nodes.py`, `tool_categories.py`, `prompts.py`, `backend/tests/test_prompt_tool_sync.py` + 6 fixtures; **Create:** `backend/app/services/chat/knowledge_profiles/reporting.yaml`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_report_tool_registration.py
from app.mcp.registry import TOOL_REGISTRY
from app.services.chat.nodes import ALLOWED_CHAT_TOOLS
from app.services.chat.tool_categories import categorize
from app.services.chat.tools import build_local_tool_definitions, _LOCAL_NAME_MAP

def test_report_compose_registered_and_visible():
    assert "report.compose" in TOOL_REGISTRY
    assert "report.export" not in TOOL_REGISTRY
    assert "report.compose" in ALLOWED_CHAT_TOOLS
    names = {t["name"] for t in build_local_tool_definitions()}
    assert "report_compose" in names
    assert _LOCAL_NAME_MAP["report_compose"] == "report.compose"

def test_report_compose_categorized():
    assert categorize("report_compose") == "report"
```

- [ ] **Step 2: Run to verify it fails** → FAIL

- [ ] **Step 3: Apply the exact edits**

1. `backend/app/mcp/tools/report_export.py` — replace whole body (keep filename to avoid import churn):
```python
async def execute(params: dict, context: dict | None = None, **kwargs) -> dict:
    """Compose a publishable report from prior tool results in this turn."""
    from app.services.report.report_service import compose_report
    from app.services.chat.tool_call_results import resolve_result_payload  # see note
    ctx = context or {}
    db = ctx["db"]
    tenant_id = ctx["tenant_id"]
    def resolver(rid: str) -> dict:
        return resolve_result_payload(ctx.get("conversation_id"), rid)  # full uncapped payload
    return await compose_report(
        db, tenant_id=tenant_id, title=params["title"], sections=params["sections"],
        resolver=resolver, created_by=ctx.get("actor_id"), source_run_id=ctx.get("conversation_id"),
    )
```
> **Resolver source:** `result_payload` lives on `ChatMessage.tool_calls` (built by `extract_result_payload`, tool_call_results.py). Add a `resolve_result_payload(conversation_id, result_id)` helper there that loads the assistant message(s) for the conversation and returns the FULL payload for the synthetic/aliased id. Write a failing test for it first (it's the §16.1 fix — assert it returns >50 rows when the source had >50).

2. `registry.py:271` — replace the `"report.export"` entry:
```python
    "report.compose": {
        "description": (
            "Compose a publishable report from results already produced THIS turn. "
            "Pass title + ordered sections; data sections reference a prior result by result_id "
            "(never inline numbers). Returns a report card; the report renders in the browser."
        ),
        "execute": report_export.execute,
        "params_schema": {
            "title": {"type": "string", "required": True, "description": "Report title"},
            "sections": {"type": "array", "required": True, "description": "Ordered report sections (see reporting profile)"},
        },
    },
```

3. `governance.py:64` — **DELETE** the entire `"report.export": {...}` `TOOL_CONFIGS` entry (so params pass through unfiltered, like `metric.compute`). **Trap:** if kept/renamed without fixing `allowlisted_params`, `title`/`sections` are silently stripped.

4. `nodes.py:44` — `"report.export",` → `"report.compose",` in `ALLOWED_CHAT_TOOLS`.

5. `tool_categories.py` — add `"report"` to the `Category` Literal (lines 14-24, it is a CLOSED union); then add before the closing brace of `_EXACT`: `"report_compose": "report",` and `"report.compose": "report",`.

6. `prompts.py:110` — change the hardcoded `"report.export"` advertise string → `"report.compose"` (or remove).

7. `backend/tests/test_prompt_tool_sync.py:125` — rename `"report_export"` → `"report_compose"` in the hardcoded known-tool set (CI invariant; the `_TOOL_NAME_RE` matches `report_compose`).

8. Fixtures hardcoding the old name (rename `report.export`→`report.compose` / `report_export`→`report_compose`): `tests/test_mcp_client.py:31`, `tests/test_chat_orchestrator.py:40`, `tests/test_chat_security.py:36`, `tests/test_mcp.py:228`, `tests/services/chat/test_metric_tool_categorization.py:33`, `tests/test_chat_tools.py:34` + `:249`.
> Run `grep -rn 'report.export\|report_export' backend/app backend/tests` and confirm ZERO hits before claiming done.

9. Create `backend/app/services/chat/knowledge_profiles/reporting.yaml`:
```yaml
profile_id: reporting
display_name: "Report Composer"
trigger_tools:
  - report_compose
prompt_fragment: |
  ## Composing Reports
  When the user asks for a "report" that tells a story over data you have ALREADY computed
  this turn, call `report_compose` with a title and an ordered `sections` list.
  - Data sections (`table`, `chart`, `metric_headline`) reference a prior result by `result_id`.
    NEVER inline numbers — the backend injects the real frozen values.
  - `narrative` sections may embed `{{result:<id>.<field>}}` or `{{metric:<id>}}`; you write the
    sentence, the backend fills the figure. Do not write raw numbers in prose.
  - The tool returns a report card the user clicks to view; do NOT restate the figures or URL.
rag_partitions: []
```

- [ ] **Step 4: Run tests** (the new registration test + the renamed sync test + full chat-tools suite)

```bash
backend/.venv/bin/python -m pytest backend/tests/test_report_tool_registration.py backend/tests/test_prompt_tool_sync.py backend/tests/test_chat_tools.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A backend/app/mcp backend/app/services/chat backend/tests
git commit -m "feat(reports): repurpose report.export -> report.compose tool + reporting profile"
```

---

## Task 7: `report_ready` SSE interception branch

**Files:** Modify `backend/app/services/chat/orchestrator.py`; Test: `backend/tests/test_report_interception.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_report_interception.py
import json
from app.services.chat.orchestrator import _intercept_tool_result

def test_report_ready_event_and_condensed_has_no_numbers():
    result = json.dumps({"report_id": "abc", "title": "Q2 Review", "section_count": 5})
    event_type, sse, condensed = _intercept_tool_result("report_compose", result)
    assert event_type == "report_ready"
    assert sse["report_id"] == "abc" and sse["title"] == "Q2 Review"
    assert "url" in sse
    assert "1.2M" not in condensed  # no figures; just title/id/section_count + a 'do not restate' note
```

- [ ] **Step 2: Run to verify it fails** → FAIL

- [ ] **Step 3: Implement** — add an early branch in `_intercept_tool_result` (orchestrator.py ~:736, modeled on the docs_link branch), and add `report_ready` to `_NON_DATA_EVENTS` (orchestrator.py:1057):

```python
    # --- Report card path ---
    if tool_name in ("report_compose", "report.compose"):
        try:
            parsed = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            return None, None, result_str
        if not isinstance(parsed, dict) or parsed.get("error") is True or not parsed.get("report_id"):
            return None, None, result_str
        rid = parsed["report_id"]
        sse_event_data = {
            "report_id": rid,
            "title": parsed.get("title", "Report"),
            "url": f"/reports/{rid}",
            "section_count": parsed.get("section_count"),
        }
        condensed = json.dumps({
            "success": True, "report_id": rid, "title": parsed.get("title", ""),
            "note": ("The report card is shown to the user as a clickable card. "
                     "Confirm what the report covers in one short line — do NOT restate figures or the URL."),
        }, default=str)
        return "report_ready", sse_event_data, condensed
```
And: `_NON_DATA_EVENTS = frozenset({"sheets_link", "docs_link", "report_ready"})` so `_build_intercept_cache_entry` returns `None` for it (a composed report is not itself a cacheable data table).

- [ ] **Step 4: Run to verify it passes** → PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/chat/orchestrator.py backend/tests/test_report_interception.py
git commit -m "feat(reports): report_ready SSE interception branch (number-free condensed)"
```

---

## Task 8: `reports` API — list / get / view

**Files:** Create `backend/app/api/v1/reports.py`; Modify `backend/app/api/v1/router.py`; Test: `backend/tests/test_reports_api.py`

`get_current_user` already calls `set_tenant_context` → endpoints are auto-RLS-scoped; a cross-tenant id is invisible → `.scalar_one_or_none()` None → 404 (spec §11). `/view` returns `text/html`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_reports_api.py
async def test_view_returns_html_and_cross_tenant_404(client, db):
    from tests.conftest import create_test_tenant, create_test_user, make_auth_headers
    from app.core.database import set_tenant_context
    from app.models.report import Report
    ta = await create_test_tenant(db, name="A"); ua = await create_test_user(db, tenant_id=ta.id)
    tb = await create_test_tenant(db, name="B"); ub = await create_test_user(db, tenant_id=tb.id)
    await set_tenant_context(db, str(ta.id))
    r = Report(tenant_id=ta.id, title="A", spec_json={"sections": []},
               rendered_html="<!DOCTYPE html><html><body>HELLO</body></html>", created_by=ua.id)
    db.add(r); await db.flush(); await db.commit()
    # owner can view
    resp = await client.get(f"/api/v1/reports/{r.id}/view", headers=await make_auth_headers(ua))
    assert resp.status_code == 200 and "text/html" in resp.headers["content-type"] and "HELLO" in resp.text
    # other tenant gets 404 (RLS-invisible)
    resp_b = await client.get(f"/api/v1/reports/{r.id}/view", headers=await make_auth_headers(ub))
    assert resp_b.status_code == 404
```

- [ ] **Step 2: Run to verify it fails** → FAIL

- [ ] **Step 3: Implement**

```python
# backend/app/api/v1/reports.py
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.report import Report
from app.models.user import User
from app.schemas.report import ReportResponse

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("", response_model=list[ReportResponse])
async def list_reports(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    rows = (await db.execute(select(Report).order_by(Report.created_at.desc()))).scalars().all()
    return [ReportResponse.model_validate(r) for r in rows]


async def _get_owned(db: AsyncSession, report_id: str) -> Report:
    try:
        rid = uuid.UUID(report_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    row = (await db.execute(select(Report).where(Report.id == rid))).scalar_one_or_none()
    if row is None:  # RLS-invisible cross-tenant rows land here too → 404 (no existence disclosure)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    return row


@router.get("/{report_id}", response_model=ReportResponse)
async def get_report(
    report_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return ReportResponse.model_validate(await _get_owned(db, report_id))


@router.get("/{report_id}/view")
async def view_report(
    report_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = await _get_owned(db, report_id)
    return Response(content=row.rendered_html, media_type="text/html")
```

`router.py`: add `reports,` to the `from app.api.v1 import (...)` tuple (after `reconciliation,`) and `api_router.include_router(reports.router)` at the end.

- [ ] **Step 4: Run to verify it passes** → PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/v1/reports.py backend/app/api/v1/router.py backend/tests/test_reports_api.py
git commit -m "feat(reports): list/get/view API (RLS-scoped, cross-tenant 404)"
```

---

## Task 9: Frontend — `report_ready` stream plumbing

**Files:** Modify `frontend/src/lib/chat-stream.ts`; Test: `frontend/src/lib/__tests__/chat-stream.report.test.ts`

- [ ] **Step 1: Write the failing test**

```ts
// frontend/src/lib/__tests__/chat-stream.report.test.ts
import { describe, it, expect } from "vitest";
import { normalizeStreamEvent } from "@/lib/chat-stream";

describe("report_ready", () => {
  it("coerces a report_ready event", () => {
    const ev = normalizeStreamEvent({ type: "report_ready", data: { report_id: "abc", title: "Q2", url: "/reports/abc", section_count: 5 } });
    expect(ev).toEqual({ type: "report_ready", data: { report_id: "abc", title: "Q2", url: "/reports/abc", section_count: 5 } });
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && npx vitest run src/lib/__tests__/chat-stream.report.test.ts` → FAIL

- [ ] **Step 3: Apply edits** (mirror the verbatim `docs_link` arms):
- After `DocsLinkData` (chat-stream.ts:81): `export interface ReportReadyData { report_id: string; title: string; url: string; section_count?: number; }`
- `StreamBlock` union (~:91): `| { type: "report_ready"; data: ReportReadyData; id: string }`
- `ChatStreamEvent` union (~:104): `| { type: "report_ready"; data: ReportReadyData }`
- `StreamHandlers` (~:128): `onReportReady?: (data: ReportReadyData) => void;`
- `consumeChatStream` dispatch (~:247, after docs_link arm): `else if (event.type === "report_ready") { handlers.onReportReady?.(event.data); }`
- `normalizeStreamEvent` (~:378, after docs_link `if`):
```ts
  if (type === "report_ready" && data.data && typeof data.data === "object") {
    const d = data.data as Record<string, unknown>;
    return { type, data: {
      report_id: String(d.report_id || ""),
      title: String(d.title || "Report"),
      url: String(d.url || ""),
      section_count: typeof d.section_count === "number" ? d.section_count : undefined,
    } };
  }
```

- [ ] **Step 4: Run to verify it passes** → PASS

- [ ] **Step 5: Commit**

```bash
cd frontend && git add src/lib/chat-stream.ts src/lib/__tests__/chat-stream.report.test.ts
git commit -m "feat(reports-fe): report_ready stream event plumbing"
```

---

## Task 10: Frontend — `ReportReadyCard` + message-list + chat-page wiring

**Files:** Create `frontend/src/components/chat/report-ready-card.tsx`; Modify `message-list.tsx`, `chat/page.tsx`; Test: `report-ready-card.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// frontend/src/components/chat/__tests__/report-ready-card.test.tsx
import { render, screen } from "@testing-library/react";
import { ReportReadyCard } from "@/components/chat/report-ready-card";

it("links in-app to the report", () => {
  render(<ReportReadyCard data={{ report_id: "abc", title: "Q2 Review", url: "/reports/abc" }} />);
  const link = screen.getByRole("link", { name: /open.*report|q2 review/i });
  expect(link).toHaveAttribute("href", "/reports/abc");   // in-app, NOT target=_blank
});
```

- [ ] **Step 2: Run to verify it fails** → FAIL

- [ ] **Step 3: Implement the card** (template: `docs-link-card.tsx`, but in-app `<Link>`, no `target=_blank`):

```tsx
// frontend/src/components/chat/report-ready-card.tsx
"use client";
import Link from "next/link";
import { FileBarChart, ArrowRight } from "lucide-react";
import type { ReportReadyData } from "@/lib/chat-stream";

export function ReportReadyCard({ data }: { data: ReportReadyData }) {
  return (
    <Link href={`/reports/${data.report_id}`} aria-label={`Open report ${data.title}`}
      className="flex items-center gap-3 rounded-xl border bg-card p-4 shadow-soft hover:bg-accent/50 transition-colors">
      <FileBarChart aria-hidden className="h-5 w-5 text-indigo-600 shrink-0" />
      <div className="flex-1 min-w-0">
        <p className="text-[15px] font-medium text-foreground truncate">{data.title}</p>
        <p className="text-[13px] text-muted-foreground truncate">Open report</p>
      </div>
      <ArrowRight aria-hidden className="h-4 w-4 text-muted-foreground shrink-0" />
    </Link>
  );
}
```

`message-list.tsx`: import the card (:27) + `ReportReadyData` type (:13); add a `case "report_ready":` to the streamBlocks switch (~:1089) rendering `<ReportReadyCard data={block.data} />`; thread `reportReady?: Map<string, ReportReadyData>` prop (mirror `docsLinks` at :640/:673/:941) + the `reportReadyData` AssistantMessageRow prop + render block (mirror :1146/:1165/:1347).

`chat/page.tsx`: add `ReportReadyData` import (:8); `const [reportReady,setReportReady]=useState<ReportReadyData|null>(null)` + `reportReadyRef=useRef<Map<string,ReportReadyData>>(new Map())` (:49-50); the persisted-event hydration arm (:176-178); `onReportReady` handler (:250) mirroring `onDocsLink`; the terminal-message flush (:337-339); pass `reportReady={reportReadyRef.current}` to `<MessageList>` (:658).
> **Trap:** wire BOTH render sites (live switch + persisted Map) AND all three state paths (useState + ref + hydration) or the card vanishes on reload.

- [ ] **Step 4: Run to verify it passes**

Run: `cd frontend && npx vitest run src/components/chat/__tests__/report-ready-card.test.tsx` → PASS

- [ ] **Step 5: Commit**

```bash
cd frontend && git add src/components/chat/report-ready-card.tsx src/components/chat/message-list.tsx "src/app/(dashboard)/chat/page.tsx" src/components/chat/__tests__/report-ready-card.test.tsx
git commit -m "feat(reports-fe): report-ready card + message-list/chat-page wiring"
```

---

## Task 11: Frontend — reports list page + hook + NAV

**Files:** Create `frontend/src/app/(dashboard)/reports/page.tsx`, `frontend/src/hooks/use-reports.ts`; Modify `frontend/src/lib/constants.ts`; Test: `use-reports.test.tsx` (or a list-render test)

- [ ] **Step 1: Write the failing test**

```tsx
// frontend/src/app/(dashboard)/reports/__tests__/reports-page.test.tsx
import { render, screen } from "@testing-library/react";
import { vi } from "vitest";
vi.mock("@/hooks/use-reports", () => ({ useReports: () => ({ data: [{ id: "abc", title: "Q2 Review", status: "draft", version: 1, created_at: "2026-06-10T00:00:00Z" }], isLoading: false }) }));
import ReportsPage from "@/app/(dashboard)/reports/page";

it("lists reports with a link to each", () => {
  render(<ReportsPage />);
  expect(screen.getByText("Q2 Review")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /q2 review/i })).toHaveAttribute("href", "/reports/abc");
});
```

- [ ] **Step 2: Run to verify it fails** → FAIL

- [ ] **Step 3: Implement** the `useReports()` hook (React Query → `apiClient.get("/api/v1/reports")`), the list page (template: `audit/page.tsx`; `"use client"`, skeleton, `space-y-6 animate-fade-in`, each row a `<Link href={`/reports/${r.id}`}>`), and add to `constants.ts` NAV_ITEMS: `{ label: "Reports", href: "/reports", icon: "FileBarChart" as const, featureFlag: null }` (set `null` to show without a flag; confirm no `reporting` flag is required).

- [ ] **Step 4: Run to verify it passes** → PASS

- [ ] **Step 5: Commit**

```bash
cd frontend && git add "src/app/(dashboard)/reports/page.tsx" src/hooks/use-reports.ts src/lib/constants.ts "src/app/(dashboard)/reports/__tests__/reports-page.test.tsx"
git commit -m "feat(reports-fe): reports list page + hook + nav"
```

---

## Task 12: Frontend — `/reports/[id]` view (blob-URL iframe) + neubrutalism tokens

**Files:** Create `frontend/src/app/(dashboard)/reports/[id]/page.tsx`; Modify `frontend/src/app/globals.css`; Test: `report-view.test.tsx`

**iframe-auth (spec §16.2):** fetch the HTML via `apiClient` (bearer attached) → `URL.createObjectURL(new Blob([html],{type:'text/html'}))` → iframe `src`. NEVER point the iframe at the cross-origin API URL directly (no bearer → 401).

- [ ] **Step 1: Write the failing test**

```tsx
// frontend/src/app/(dashboard)/reports/[id]/__tests__/report-view.test.tsx
import { render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";
vi.mock("next/navigation", () => ({ useParams: () => ({ id: "abc" }), useRouter: () => ({ push: vi.fn() }) }));
const getHtml = vi.fn().mockResolvedValue("<!DOCTYPE html><html><body>REPORT</body></html>");
vi.mock("@/lib/api-client", () => ({ apiClient: { getText: getHtml } }));
import ReportViewPage from "@/app/(dashboard)/reports/[id]/page";

it("fetches report HTML via apiClient and renders an iframe", async () => {
  render(<ReportViewPage />);
  await waitFor(() => expect(getHtml).toHaveBeenCalledWith("/api/v1/reports/abc/view"));
  expect(document.querySelector("iframe")).toBeTruthy();
});
```

- [ ] **Step 2: Run to verify it fails** → FAIL

- [ ] **Step 3: Implement.** Add `getText(path)` to `apiClient` (a `request` variant returning `res.text()` with the same bearer/refresh logic as `get`). Implement the view page:

```tsx
// frontend/src/app/(dashboard)/reports/[id]/page.tsx
"use client";
import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { apiClient } from "@/lib/api-client";
import { Button } from "@/components/ui/button";
import { ArrowLeft } from "lucide-react";

export default function ReportViewPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let url: string | null = null;
    let cancelled = false;
    apiClient.getText(`/api/v1/reports/${id}/view`)
      .then((html) => {
        if (cancelled) return;
        url = URL.createObjectURL(new Blob([html], { type: "text/html" }));
        setBlobUrl(url);
      })
      .catch(() => !cancelled && setError("Report not found"));
    return () => { cancelled = true; if (url) URL.revokeObjectURL(url); };
  }, [id]);

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-3 border-b-[3px] border-black bg-card px-4 py-2">
        <Button variant="ghost" size="sm" onClick={() => router.back()}><ArrowLeft className="h-4 w-4 mr-1" />Back</Button>
        {/* Slice 2: Publish to Drive / Download PDF buttons (disabled here) */}
      </div>
      {error ? <div className="p-8 text-muted-foreground">{error}</div>
        : blobUrl ? <iframe src={blobUrl} title="Report" className="flex-1 w-full border-0" />
        : <div className="p-8 text-muted-foreground">Loading…</div>}
    </div>
  );
}
```

`globals.css`: add neubrutalist tokens for the app-chrome wrapper (the artifact carries its own inline CSS): `--report-bg`, `--report-border`, `--report-shadow`, `--report-accent` (the accent from `brand_color_hsl` is inlined server-side into the artifact, not here).

- [ ] **Step 4: Run to verify it passes** → PASS

- [ ] **Step 5: Commit**

```bash
cd frontend && git add "src/app/(dashboard)/reports/[id]/page.tsx" src/lib/api-client.ts src/app/globals.css "src/app/(dashboard)/reports/[id]/__tests__/report-view.test.tsx"
git commit -m "feat(reports-fe): /reports/[id] view via blob-URL iframe (authed fetch)"
```

---

## Task 13: Backend seeded-tenant e2e (compose → view lifecycle)

**Files:** Create `backend/tests/e2e/test_report_lifecycle_e2e.py` (template: `test_recon_lifecycle_e2e.py`); the T2 CI seeded-tenant gate.

- [ ] **Step 1: Write the failing e2e** — seed a tenant + an assistant message carrying a `result_payload` with >50 rows; call `compose_report` referencing it; assert (a) the persisted `reports.rendered_html` contains all rows (full, not 50-capped — the §16.1 regression guard), (b) `GET /reports/{id}/view` returns the HTML, (c) cross-tenant `GET` → 404, (d) the LLM-condensed string carried no figures.
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Make it pass** (fixes flush from earlier tasks).
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** `test(reports): seeded-tenant compose→view e2e (full-rows + RLS + trust-boundary)`

---

## Task 14: Playwright golden-path e2e

**Files:** Create `frontend/e2e/reports.spec.ts` (template: `deploy-gate.spec.ts` `injectToken`); gated in CI (not `continue-on-error`).

- [ ] **Step 1: Write the failing spec** — inject token; navigate to a seeded `/reports/[id]`; assert the iframe loads and the artifact contains a known heading + a chart `<svg>`; assert a missing-data section renders the error block (not a crash); assert the chat "Open report" card navigates in-app.
- [ ] **Step 2: Run → FAIL** (`cd frontend && npx playwright test reports.spec.ts`)
- [ ] **Step 3: Make it pass.**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** `test(reports-fe): playwright golden-path e2e (compose card → view → render)`

---

## Task 15: T2 review gates + deploy

Per CLAUDE.md "## UAT + Review" (T2) and `.claude/rules/uat-review.md`:

- [ ] **In-loop advisory (if built via Workflow):** add a final `Review` phase calling `workflow('code-review-multiangle', {diff})` (template `.claude/workflows/build-with-review.template.js`) — attaches findings, non-blocking.
- [ ] **Full backend + frontend suite green:** `backend/.venv/bin/python -m pytest backend/tests -q` and `cd frontend && npx vitest run` + `npx tsc --noEmit`.
- [ ] **Blocking pre-merge T2 gate:** `Workflow({name:"code-review-multiangle", args:{target:"feat/publishable-report-renderer"}})`. Read `status` FIRST (INCOMPLETE/PREP_FAILED ⇒ re-run); confirm `codex_used: true` (else re-run where codex is authed — `codex login`); resolve every CONFIRMED + PLAUSIBLE-major before merge.
- [ ] **grill-me** (independent-model cross-exam) on the branch before opening the PR — `dangerouslyDisableSandbox`. (Now also folded into the gate as the codex angle, but a standalone grill on the design+diff is still the strongest pre-PR check.)
- [ ] **Open PR**, push to BOTH `origin` + `framework`.
- [ ] **Apply migration 084** to staging (`.github/workflows/migrate.yml` or manual) + local docker; merging to `main` **auto-deploys staging** (watched deploy).
- [ ] **Post-deploy live smoke (T2):** the authoritative RLS proof — compose + view a report against the `uat-smoke` staging tenant, assert tenant-isolation (cross-tenant 404) + zero residue (delete the report by id). Model on `scripts/uat/recon_live_smoke.py` (slug-guarded, zero-residue).
- [ ] **Update memory:** mark Slice 1 shipped; note Slice 2 (weasyprint PDF → Drive) is the next slice.

---

## Self-Review (against the spec)

- **Spec coverage:** §3 architecture → Tasks 5–8,12; §4 trust boundary → Tasks 5,7 (+ tests); §5 data model → Task 1; §6 section contract → Tasks 2,4; §7 compose tool → Task 6; §8 SVG charts → Task 3; §9 in-browser render → Tasks 8,12; §10 neubrutalism tokens → Tasks 4,12; §11 errors → Tasks 4,5 (error section), 8 (404); §12 testing → every task + 13,14; §13 gates → Task 15; §16.1 full-rows → Task 6 resolver + Task 13 guard; §16.2 iframe → Task 12; §16.4 chart scope → Task 3. **No gaps.**
- **Placeholder scan:** none — every code step has real code; wiring tasks (6,9,10) use exact file:line edit points (complete, not vague).
- **Type consistency:** `report.compose`/`report_compose`, `ReportReadyData`/`report_ready`, `compose_report`/`assemble_spec`/`render_report_html`/`render_chart_svg`/`fill_placeholders` consistent across tasks; `result_payload` resolver named `resolve_result_payload` in Tasks 6 & 13.
- **Known follow-up (verify in execution):** `resolve_result_payload(conversation_id, result_id)` helper in `tool_call_results.py` is the §16.1 fix — write its failing test first (assert >50 rows round-trip). Confirm `alembic heads` is a single head before applying 084.
