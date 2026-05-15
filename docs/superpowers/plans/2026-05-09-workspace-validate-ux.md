# In-app Validate UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace today's `sdf validate` (Apache, shallow) with Oracle's `suitecloud project:validate --server`, surface structured hits in the runs panel + chat thread with Oracle policy citations from RAG, auto-fire on `workspace_apply_patch` and `workspace_deploy_sandbox`, and let the agent auto-propose fix patches for mechanically-fixable hits.

**Architecture:** New `ValidationHit` model + parser + auth seeder + auto-validate orchestrator. `runner_service` allowlist swap (`sdf_validate` → `suitecloud_validate`). Snapshot-hash freshness on the deploy gate. Workspace agent narrates batched-by-family with citations from `oracle/*` RAG partitions. Frontend grows a hits-table + retry button on stale/failed runs.

**Tech Stack:** Python 3.12 (FastAPI / SQLAlchemy 2.0 async / pytest), TypeScript (Next.js 14 / React Query / vitest), `@oracle/suitecloud-cli` (Node), Alembic migrations, OpenAI text-embedding-3-small (already wired for RAG retrieval).

**Spec:** `docs/superpowers/specs/2026-05-09-workspace-validate-ux-design.md` — read first if you have not.

---

## File Structure

### New files
| Path | Purpose |
|---|---|
| `backend/app/services/workspace/validate_parser.py` | Best-effort parser of `suitecloud project:validate --server` output. Returns `(list[ValidationHit], parser_version)` + raw fallback. |
| `backend/app/services/workspace/auto_validate_orchestrator.py` | Per-workspace debounce + per-changeset loop budget + finding-fingerprint dedup. |
| `backend/app/services/workspace/mechanical_fix_classifier.py` | Deny-by-default Oracle rule-ID allowlist → patch generators. |
| `backend/app/services/workspace/suitecloud_auth_seeder.py` | Per-tenant credential file write for the `suitecloud` CLI. |
| `backend/alembic/versions/074_validation_hits.py` | DB migration: `validation_hits` table + columns on `workspace_runs`. |
| `backend/tests/services/workspace/test_validate_parser.py` | Parser unit tests (clean / errors / warnings / mixed / malformed). |
| `backend/tests/services/workspace/test_auto_validate_orchestrator.py` | Orchestrator unit tests (debounce / loop budget / fingerprint dedup). |
| `backend/tests/services/workspace/test_mechanical_fix_classifier.py` | Classifier unit tests (deny-by-default + allowlist). |
| `backend/tests/services/workspace/test_suitecloud_auth_seeder.py` | Auth seeder unit tests (credential format + refresh). |
| `backend/tests/services/workspace/test_validate_runner_integration.py` | Integration test: end-to-end auto-validate flow. |
| `backend/tests/services/workspace/test_deploy_freshness.py` | Snapshot-hash deploy gate tests. |
| `backend/tests/services/workspace/fixtures/suitecloud_validate_clean.txt` | Parser fixture: clean output. |
| `backend/tests/services/workspace/fixtures/suitecloud_validate_errors.txt` | Parser fixture: errors only. |
| `backend/tests/services/workspace/fixtures/suitecloud_validate_warnings.txt` | Parser fixture: warnings only. |
| `backend/tests/services/workspace/fixtures/suitecloud_validate_mixed.txt` | Parser fixture: errors + warnings. |
| `backend/tests/services/workspace/fixtures/suitecloud_validate_malformed.txt` | Parser fixture: malformed. |
| `backend/tests/agent_benchmarks/benchmark_cases/vs_mcp/workspace_owasp_validate.yaml` | Benchmark case: agent applies patch with OWASP injection, validates, narrates citation, proposes fix. |
| `frontend/src/components/workspace/validation-hits-table.tsx` | Hits-table component (file / line / severity / code / message). |
| `frontend/src/components/workspace/__tests__/validation-hits-table.test.tsx` | Vitest coverage for hits-table render + retry-button visibility. |

### Modified files
| Path | Change |
|---|---|
| `backend/app/models/workspace.py` | New `ValidationHit` model + `validator_engine`, `parser_version`, `has_errors`, `has_warnings`, `gate_status`, `snapshot_hash` columns on `WorkspaceRun`. |
| `backend/app/services/runner_service.py` | Allowlist swap: replace `sdf_validate` with `suitecloud_validate` (180s, `["suitecloud", "project:validate", "--server"]`). New `_execute_suitecloud_validate_run` branch in `execute_run` that invokes auth seeder, runs subprocess, parses output, persists `ValidationHit` rows + new run-record columns. |
| `backend/app/services/deploy_service.py` | Switch validate gate from `sdf_validate` → `suitecloud_validate`. Add `_get_fresh_validate_run` keyed on `snapshot_hash`. Read `gate_status` instead of `status`. |
| `backend/app/mcp/tools/workspace_tools.py` | `workspace_apply_patch` (or its post-apply hook) calls `auto_validate_orchestrator.enqueue(...)` after a successful changeset apply. |
| `backend/app/services/chat/agents/workspace_agent.py` | New post-validate narration logic in the system prompt + `mechanical_fix_classifier` integration for auto-propose. Add `workspace_run_validate` to allowed tools. |
| `backend/Dockerfile.prod` | Install `@oracle/suitecloud-cli` globally (`npm install -g @oracle/suitecloud-cli`). |
| `backend/.dockerignore` | (No changes required — backend image already has node available; we only add a global package.) |
| `backend/app/api/v1/workspaces.py` | `runs` response schema includes the new `findings_json` (list of `ValidationHit`) + `gate_status` fields. |
| `frontend/src/lib/types.ts` | `ValidationHit`, `ValidatorEngine`, `RunGateStatus` types. Extend `WorkspaceRun` with new fields. |
| `frontend/src/components/workspace/runs-panel.tsx` | Render `ValidationHitsTable` under each `suitecloud_validate` run. Show "Retry validate" button when `status === "failed"` or `gate_status === "stale"`. |
| `frontend/src/hooks/use-runs.ts` | New `useRetryValidate(runId)` mutation + invalidation. |

---

## Task Sequence (13 tasks, sequential)

Tasks 1–3 are foundational and must land first. Task 5 (Dockerfile) can land anytime before Task 4 ships to staging. Tasks 6–10 build on 1–4. Tasks 11 (frontend) and 12 (integration) can be parallelized once Task 10 is green if a second worktree exists; otherwise sequential is fine.

---

### Task 1: Migration + ValidationHit model

**Files:**
- Create: `backend/alembic/versions/074_validation_hits.py`
- Modify: `backend/app/models/workspace.py`
- Test: `backend/tests/models/test_validation_hit_model.py`

- [ ] **Step 1: Write the failing model test**

```python
# backend/tests/models/test_validation_hit_model.py
"""Validation-hit model + migration tests."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.workspace import ValidationHit, Workspace, WorkspaceRun


@pytest.mark.asyncio
async def test_validation_hit_persists_with_run(db: AsyncSession, seeded_workspace: Workspace) -> None:
    run = WorkspaceRun(
        tenant_id=seeded_workspace.tenant_id,
        workspace_id=seeded_workspace.id,
        run_type="suitecloud_validate",
        status="failed",
        triggered_by=seeded_workspace.created_by,
        validator_engine="suitecloud_server",
        parser_version="1.0.0",
        has_errors=True,
        has_warnings=False,
        gate_status="block",
        snapshot_hash="a" * 64,
    )
    db.add(run)
    await db.flush()

    hit = ValidationHit(
        tenant_id=seeded_workspace.tenant_id,
        run_id=run.id,
        file_path="src/Suitelets/foo.js",
        line=42,
        severity="error",
        code="OWASP-A03",
        rule_id="netsuite-owasp-secure-coding/injection",
        message="Unsanitized user input flowed into N/query",
        fingerprint="0123456789abcdef" * 4,
    )
    db.add(hit)
    await db.flush()

    fetched = (await db.execute(select(ValidationHit).where(ValidationHit.run_id == run.id))).scalar_one()
    assert fetched.severity == "error"
    assert fetched.fingerprint == "0123456789abcdef" * 4


@pytest.mark.asyncio
async def test_run_gate_status_columns_exist(db: AsyncSession) -> None:
    inspector = inspect((await db.connection()).sync_engine)
    cols = {c["name"] for c in inspector.get_columns("workspace_runs")}
    assert {"validator_engine", "parser_version", "has_errors", "has_warnings", "gate_status", "snapshot_hash"}.issubset(cols)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/models/test_validation_hit_model.py -v`
Expected: ImportError on `ValidationHit` (model doesn't exist).

- [ ] **Step 3: Add model + run-record columns to `backend/app/models/workspace.py`**

Append below the existing `WorkspaceArtifact` class:

```python
class ValidationHit(Base, UUIDPrimaryKeyMixin):
    """One structured validate finding (file / line / severity / code / message).

    Belongs to a `WorkspaceRun` with `run_type == "suitecloud_validate"`.
    """

    __tablename__ = "validation_hits"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspace_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)  # error | warning | info | parser_error
    code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    rule_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default="now()", nullable=False)

    run: Mapped["WorkspaceRun"] = relationship("WorkspaceRun", back_populates="validation_hits")
```

Then extend `WorkspaceRun` (around the existing column block, lines 117-139) by adding these columns inside the class body and the back-populating relationship:

```python
    # Validate-UX additions (PR: feat/workspace-validate-ux)
    validator_engine: Mapped[str | None] = mapped_column(String(32), nullable=True)  # suitecloud_server | sdf_legacy
    parser_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    has_errors: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    has_warnings: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    gate_status: Mapped[str | None] = mapped_column(String(32), nullable=True)  # pass | block | stale | unknown
    snapshot_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    validation_hits: Mapped[list["ValidationHit"]] = relationship(
        "ValidationHit", back_populates="run", cascade="all, delete-orphan"
    )
```

- [ ] **Step 4: Write the Alembic migration**

```python
# backend/alembic/versions/074_validation_hits.py
"""validation_hits + workspace_runs validate columns

Revision ID: 074_validation_hits
Revises: 073_chat_disclosure_events
Create Date: 2026-05-09
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "074_validation_hits"
down_revision = "073_chat_disclosure_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "validation_hits",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False, index=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("workspace_runs.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("file_path", sa.Text(), nullable=True),
        sa.Column("line", sa.Integer(), nullable=True),
        sa.Column("severity", sa.String(32), nullable=False),
        sa.Column("code", sa.String(128), nullable=True),
        sa.Column("rule_id", sa.String(256), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.add_column("workspace_runs", sa.Column("validator_engine", sa.String(32), nullable=True))
    op.add_column("workspace_runs", sa.Column("parser_version", sa.String(16), nullable=True))
    op.add_column("workspace_runs", sa.Column("has_errors", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("workspace_runs", sa.Column("has_warnings", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("workspace_runs", sa.Column("gate_status", sa.String(32), nullable=True))
    op.add_column("workspace_runs", sa.Column("snapshot_hash", sa.String(64), nullable=True))
    op.create_index("ix_workspace_runs_snapshot_hash", "workspace_runs", ["snapshot_hash"])


def downgrade() -> None:
    op.drop_index("ix_workspace_runs_snapshot_hash", table_name="workspace_runs")
    op.drop_column("workspace_runs", "snapshot_hash")
    op.drop_column("workspace_runs", "gate_status")
    op.drop_column("workspace_runs", "has_warnings")
    op.drop_column("workspace_runs", "has_errors")
    op.drop_column("workspace_runs", "parser_version")
    op.drop_column("workspace_runs", "validator_engine")
    op.drop_table("validation_hits")
```

- [ ] **Step 5: Apply migration locally + verify**

Run: `cd backend && .venv/bin/alembic upgrade head && .venv/bin/python -m pytest tests/models/test_validation_hit_model.py -v`
Expected: PASS (both tests).

- [ ] **Step 6: Commit**

```bash
git add backend/alembic/versions/074_validation_hits.py backend/app/models/workspace.py backend/tests/models/test_validation_hit_model.py
git commit -m "feat(workspace): add ValidationHit model + workspace_runs validate columns"
```

---

### Task 2: validate_parser

**Files:**
- Create: `backend/app/services/workspace/__init__.py` (if not exists)
- Create: `backend/app/services/workspace/validate_parser.py`
- Create: `backend/tests/services/workspace/test_validate_parser.py`
- Create: `backend/tests/services/workspace/fixtures/suitecloud_validate_clean.txt`
- Create: `backend/tests/services/workspace/fixtures/suitecloud_validate_errors.txt`
- Create: `backend/tests/services/workspace/fixtures/suitecloud_validate_warnings.txt`
- Create: `backend/tests/services/workspace/fixtures/suitecloud_validate_mixed.txt`
- Create: `backend/tests/services/workspace/fixtures/suitecloud_validate_malformed.txt`

- [ ] **Step 1: Write fixture files**

Each fixture is a representative sample of `suitecloud project:validate --server` stdout. The Oracle CLI does not have a documented JSON schema, so we treat output as best-effort line-based.

```text
# backend/tests/services/workspace/fixtures/suitecloud_validate_clean.txt
INFO: Validating project against account 6738075...
INFO: Account validation complete.
INFO: Dependency validation complete.
SUCCESS: Project validation completed successfully.
```

```text
# backend/tests/services/workspace/fixtures/suitecloud_validate_errors.txt
INFO: Validating project against account 6738075...
ERROR: src/Suitelets/processOrder.js:42 [OWASP-A03] Unsanitized user input flowed into N/query.runSuiteQL.
ERROR: src/Suitelets/processOrder.js:67 [SDF-SCHEMA-001] Reference to missing custom record `customrecord_unknown`.
FAILURE: Project validation failed with 2 error(s).
```

```text
# backend/tests/services/workspace/fixtures/suitecloud_validate_warnings.txt
INFO: Validating project against account 6738075...
WARNING: src/UserEvents/auditLog.js:18 [SUITESCRIPT-DEPRECATED-2X] nlapiSearchRecord is deprecated in 2.1; use N/search.
WARNING: src/UserEvents/auditLog.js:34 [GOVERNANCE-CHECK] Missing remainingUsage check inside loop.
SUCCESS: Project validation completed successfully (with warnings).
```

```text
# backend/tests/services/workspace/fixtures/suitecloud_validate_mixed.txt
INFO: Validating project against account 6738075...
WARNING: src/Suitelets/processOrder.js:12 [SUITESCRIPT-DEPRECATED-2X] nlapiLoadRecord is deprecated in 2.1.
ERROR: src/Suitelets/processOrder.js:42 [OWASP-A03] Unsanitized user input flowed into N/query.runSuiteQL.
FAILURE: Project validation failed with 1 error(s) and 1 warning(s).
```

```text
# backend/tests/services/workspace/fixtures/suitecloud_validate_malformed.txt
Some unexpected output that does not match the documented format.
[unparseable garbage line]
```

- [ ] **Step 2: Write the failing parser test**

```python
# backend/tests/services/workspace/test_validate_parser.py
"""Validate-output parser unit tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services.workspace.validate_parser import (
    PARSER_VERSION,
    ValidationParseResult,
    parse_suitecloud_validate_output,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parser_handles_clean_run() -> None:
    result = parse_suitecloud_validate_output(_load("suitecloud_validate_clean.txt"))
    assert result.hits == []
    assert result.has_errors is False
    assert result.has_warnings is False
    assert result.parser_version == PARSER_VERSION


def test_parser_extracts_errors() -> None:
    result = parse_suitecloud_validate_output(_load("suitecloud_validate_errors.txt"))
    assert len(result.hits) == 2
    assert result.has_errors is True
    assert result.has_warnings is False
    first = result.hits[0]
    assert first.severity == "error"
    assert first.file_path == "src/Suitelets/processOrder.js"
    assert first.line == 42
    assert first.code == "OWASP-A03"
    assert "Unsanitized" in first.message


def test_parser_extracts_warnings() -> None:
    result = parse_suitecloud_validate_output(_load("suitecloud_validate_warnings.txt"))
    assert len(result.hits) == 2
    assert result.has_errors is False
    assert result.has_warnings is True
    assert all(h.severity == "warning" for h in result.hits)


def test_parser_handles_mixed_severity() -> None:
    result = parse_suitecloud_validate_output(_load("suitecloud_validate_mixed.txt"))
    assert result.has_errors is True
    assert result.has_warnings is True
    severities = {h.severity for h in result.hits}
    assert severities == {"error", "warning"}


def test_parser_falls_back_on_malformed() -> None:
    raw = _load("suitecloud_validate_malformed.txt")
    result = parse_suitecloud_validate_output(raw)
    assert len(result.hits) == 1
    assert result.hits[0].severity == "parser_error"
    assert result.hits[0].message  # raw output is preserved in the synthetic hit
    assert result.raw_output == raw


def test_parser_handles_empty_input() -> None:
    result = parse_suitecloud_validate_output("")
    assert result.hits == []
    assert result.has_errors is False
    assert result.has_warnings is False


def test_fingerprint_is_stable_across_runs() -> None:
    raw = _load("suitecloud_validate_errors.txt")
    a = parse_suitecloud_validate_output(raw)
    b = parse_suitecloud_validate_output(raw)
    assert [h.fingerprint for h in a.hits] == [h.fingerprint for h in b.hits]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/services/workspace/test_validate_parser.py -v`
Expected: ImportError (`validate_parser` module doesn't exist).

- [ ] **Step 4: Implement the parser**

```python
# backend/app/services/workspace/__init__.py
"""Workspace service helpers (validate UX, runner orchestration)."""
```

```python
# backend/app/services/workspace/validate_parser.py
"""Best-effort parser for `suitecloud project:validate --server` stdout.

Oracle's CLI does not document a stable JSON diagnostic schema. This parser
walks the stdout looking for `<SEVERITY>: <file>:<line> [<code>] <message>`
lines. Anything that doesn't match is preserved in `raw_output`. If NO lines
match a known severity prefix and the input is non-empty, we synthesize a
single `parser_error` hit so the issue surfaces to the user.

Hit fingerprinting: `sha256(file + ":" + line + ":" + code + ":" + message)`.
Used by the orchestrator to dedup repeat auto-propose attempts.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Final

PARSER_VERSION: Final[str] = "1.0.0"

# Match: ERROR: src/foo.js:42 [CODE-001] message...
_LINE_RE = re.compile(
    r"^(?P<severity>ERROR|WARNING|INFO):\s+"
    r"(?P<file>[^\s:][^:]*):(?P<line>\d+)\s+"
    r"\[(?P<code>[A-Za-z0-9._\-]+)\]\s+"
    r"(?P<message>.*?)$"
)


@dataclass(frozen=True)
class ParsedHit:
    severity: str  # error | warning | info | parser_error
    file_path: str | None
    line: int | None
    code: str | None
    message: str
    fingerprint: str


@dataclass
class ValidationParseResult:
    hits: list[ParsedHit] = field(default_factory=list)
    has_errors: bool = False
    has_warnings: bool = False
    raw_output: str = ""
    parser_version: str = PARSER_VERSION


def _fingerprint(file_path: str | None, line: int | None, code: str | None, message: str) -> str:
    payload = f"{file_path or ''}:{line or 0}:{code or ''}:{message}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_suitecloud_validate_output(stdout: str) -> ValidationParseResult:
    """Parse `suitecloud project:validate --server` stdout into structured hits.

    Returns an empty hit list for clean runs. Synthesizes a single `parser_error`
    hit when the input is non-empty but no lines match the expected format.
    """
    result = ValidationParseResult(raw_output=stdout)
    if not stdout.strip():
        return result

    for line in stdout.splitlines():
        match = _LINE_RE.match(line)
        if not match:
            continue
        severity_word = match.group("severity").lower()
        if severity_word not in ("error", "warning", "info"):
            continue
        line_no = int(match.group("line"))
        file_path = match.group("file")
        code = match.group("code")
        message = match.group("message").strip()
        result.hits.append(
            ParsedHit(
                severity=severity_word,
                file_path=file_path,
                line=line_no,
                code=code,
                message=message,
                fingerprint=_fingerprint(file_path, line_no, code, message),
            )
        )
        if severity_word == "error":
            result.has_errors = True
        elif severity_word == "warning":
            result.has_warnings = True

    if not result.hits:
        result.hits.append(
            ParsedHit(
                severity="parser_error",
                file_path=None,
                line=None,
                code=None,
                message="suitecloud validate output did not match expected format; raw stdout preserved.",
                fingerprint=_fingerprint(None, None, "PARSER_ERROR", stdout[:256]),
            )
        )
    return result
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && .venv/bin/python -m pytest tests/services/workspace/test_validate_parser.py -v`
Expected: 7 PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/workspace/__init__.py backend/app/services/workspace/validate_parser.py backend/tests/services/workspace/test_validate_parser.py backend/tests/services/workspace/fixtures/suitecloud_validate_*.txt
git commit -m "feat(workspace): suitecloud project:validate output parser + fixtures"
```

---

### Task 3: suitecloud_auth_seeder

**Files:**
- Create: `backend/app/services/workspace/suitecloud_auth_seeder.py`
- Create: `backend/tests/services/workspace/test_suitecloud_auth_seeder.py`

The `suitecloud` CLI uses its own credential file at `~/.suitecloud-sdk/credentials/<project>.json`. Format is documented at https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4719750997.html. We bridge from the `connections` table (encrypted OAuth2 tokens) by writing a token-style entry the CLI accepts.

- [ ] **Step 1: Write the failing seeder test**

```python
# backend/tests/services/workspace/test_suitecloud_auth_seeder.py
"""Auth seeder tests."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from app.services.workspace.suitecloud_auth_seeder import (
    AuthSeederError,
    seed_credentials_for_run,
)


@pytest.mark.asyncio
async def test_seeder_writes_credential_file(tmp_path: Path, db, seeded_connection_with_oauth) -> None:
    auth_dir = tmp_path / ".suitecloud-sdk" / "credentials"
    creds_path = await seed_credentials_for_run(
        db=db,
        tenant_id=seeded_connection_with_oauth.tenant_id,
        auth_root=tmp_path,
        project_id="ws-1",
    )
    assert creds_path.exists()
    payload = json.loads(creds_path.read_text())
    assert payload["accountId"] == seeded_connection_with_oauth.account_id
    assert payload["authType"] == "tba"  # or oauth2 — depends on CLI version
    assert "token" in payload or "oauth2" in payload


@pytest.mark.asyncio
async def test_seeder_raises_when_no_connection(tmp_path: Path, db) -> None:
    with pytest.raises(AuthSeederError, match="no active NetSuite connection"):
        await seed_credentials_for_run(
            db=db,
            tenant_id=uuid.uuid4(),
            auth_root=tmp_path,
            project_id="ws-1",
        )


@pytest.mark.asyncio
async def test_seeder_refreshes_expired_token(tmp_path: Path, db, seeded_connection_with_expired_token) -> None:
    creds_path = await seed_credentials_for_run(
        db=db,
        tenant_id=seeded_connection_with_expired_token.tenant_id,
        auth_root=tmp_path,
        project_id="ws-1",
    )
    payload = json.loads(creds_path.read_text())
    # Refreshed token must not equal the expired one
    assert payload["token"]["access_token"] != seeded_connection_with_expired_token.expired_access_token
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/services/workspace/test_suitecloud_auth_seeder.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement the seeder**

```python
# backend/app/services/workspace/suitecloud_auth_seeder.py
"""Bridge from our `connections` table to the suitecloud CLI's credential format.

The Oracle suitecloud CLI expects credentials at:
  $HOME/.suitecloud-sdk/credentials/<project_id>.json

Our `connections` table holds encrypted OAuth2 tokens per tenant. This module
decrypts the active connection, refreshes if expired, and writes the CLI's
expected JSON shape. Per-run write (not pod-startup) so token refresh races
with long-running jobs are avoided.

CLI version contract: documented for `@oracle/suitecloud-cli` >=2.0. If the CLI
upgrades, re-verify the credential file shape.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import decrypt_credentials
from app.models.connection import Connection
from app.services.netsuite_token_service import get_valid_token

logger = structlog.get_logger()


class AuthSeederError(Exception):
    """Raised when credentials cannot be seeded for a runner subprocess."""


async def seed_credentials_for_run(
    *,
    db: AsyncSession,
    tenant_id: uuid.UUID,
    auth_root: Path,
    project_id: str,
) -> Path:
    """Write a suitecloud-CLI credential file for `tenant_id`.

    Returns the absolute path of the written credential file. Caller passes
    `auth_root` as the runner's per-run HOME (e.g. tmp_dir) so multiple
    tenants never share a credential cache.
    """
    result = await db.execute(
        select(Connection)
        .where(Connection.tenant_id == tenant_id, Connection.is_active.is_(True))
        .limit(1)
    )
    connection = result.scalar_one_or_none()
    if connection is None:
        raise AuthSeederError(f"no active NetSuite connection for tenant {tenant_id}")

    creds = decrypt_credentials(connection.encrypted_credentials)

    # Refresh if expired or near-expiry; raises on failure.
    fresh_token = await get_valid_token(db=db, connection=connection, decrypted_credentials=creds)

    cred_dir = auth_root / ".suitecloud-sdk" / "credentials"
    cred_dir.mkdir(parents=True, exist_ok=True)
    cred_path = cred_dir / f"{project_id}.json"

    payload: dict[str, Any] = {
        "accountId": creds["account_id"],
        "authType": "oauth2",
        "oauth2": {
            "clientId": creds["client_id"],
            "accessToken": fresh_token["access_token"],
            "refreshToken": fresh_token["refresh_token"],
            "tokenExpiry": fresh_token.get("expires_at"),
        },
    }
    cred_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    cred_path.chmod(0o600)
    logger.info("suitecloud_auth.seeded", tenant_id=str(tenant_id), project_id=project_id)
    return cred_path
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/python -m pytest tests/services/workspace/test_suitecloud_auth_seeder.py -v`
Expected: 3 PASS. (Fixtures `seeded_connection_with_oauth` and `seeded_connection_with_expired_token` go in `tests/conftest.py` — see existing connection fixtures for the pattern.)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/workspace/suitecloud_auth_seeder.py backend/tests/services/workspace/test_suitecloud_auth_seeder.py
git commit -m "feat(workspace): suitecloud CLI credential seeder for per-run auth"
```

---

### Task 4: Runner integration (suitecloud_validate run_type)

**Files:**
- Modify: `backend/app/services/runner_service.py:36-53` (allowlist) + `:439-519` (execute_run)
- Create: `backend/tests/services/workspace/test_validate_runner_integration.py`

- [ ] **Step 1: Write the failing runner integration test**

```python
# backend/tests/services/workspace/test_validate_runner_integration.py
"""Runner integration: end-to-end suitecloud_validate run_type."""
from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models.workspace import ValidationHit, WorkspaceRun
from app.services import runner_service


@pytest.mark.asyncio
async def test_suitecloud_validate_run_in_allowlist() -> None:
    cfg = runner_service.validate_run_type("suitecloud_validate")
    assert cfg["cmd"] == ["suitecloud", "project:validate", "--server"]
    assert cfg["timeout"] == 180


@pytest.mark.asyncio
async def test_sdf_validate_removed_from_allowlist() -> None:
    with pytest.raises(runner_service.CommandNotAllowedError):
        runner_service.validate_run_type("sdf_validate")


@pytest.mark.asyncio
async def test_execute_run_persists_validation_hits(db, seeded_workspace_with_changeset) -> None:
    run = await runner_service.create_run(
        db=db,
        tenant_id=seeded_workspace_with_changeset.tenant_id,
        workspace_id=seeded_workspace_with_changeset.id,
        run_type="suitecloud_validate",
        triggered_by=seeded_workspace_with_changeset.created_by,
        changeset_id=seeded_workspace_with_changeset.changeset_id,
    )

    fixture_stdout = Path("tests/services/workspace/fixtures/suitecloud_validate_errors.txt").read_text()
    with (
        patch("app.services.runner_service._run_subprocess", new=AsyncMock(return_value=(1, fixture_stdout, ""))),
        patch("app.services.workspace.suitecloud_auth_seeder.seed_credentials_for_run", new=AsyncMock(return_value=Path("/tmp/fake.json"))),
    ):
        await runner_service.execute_run(db=db, run_id=run.id, tenant_id=run.tenant_id)

    refreshed = (await db.execute(select(WorkspaceRun).where(WorkspaceRun.id == run.id))).scalar_one()
    assert refreshed.status == "failed"
    assert refreshed.has_errors is True
    assert refreshed.gate_status == "block"
    assert refreshed.validator_engine == "suitecloud_server"
    assert refreshed.parser_version == "1.0.0"
    assert refreshed.snapshot_hash and len(refreshed.snapshot_hash) == 64

    hits = (await db.execute(select(ValidationHit).where(ValidationHit.run_id == run.id))).scalars().all()
    assert len(hits) == 2
    codes = {h.code for h in hits}
    assert codes == {"OWASP-A03", "SDF-SCHEMA-001"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/services/workspace/test_validate_runner_integration.py -v`
Expected: FAIL — `suitecloud_validate` not in allowlist + `_execute_run` doesn't know how to handle it.

- [ ] **Step 3: Replace allowlist entry in `runner_service.py`**

Edit `backend/app/services/runner_service.py` lines 36-53. Replace the `"sdf_validate"` block with a `"suitecloud_validate"` block:

```python
ALLOWED_COMMANDS: dict[str, dict] = {
    "suitecloud_validate": {
        "cmd": ["suitecloud", "project:validate", "--server"],
        "timeout": 180,
    },
    "jest_unit_test": {
        "cmd": ["npx", "jest", "--json", "--coverage"],
        "timeout": 120,
    },
    "suiteql_assertions": {
        "cmd": [],
        "timeout": 300,
    },
    "deploy_sandbox": {
        "cmd": ["suitecloud", "project:deploy", "--destinationFolder", "/SuiteScripts"],
        "timeout": 600,
    },
}
```

- [ ] **Step 4: Add a `_compute_snapshot_hash` helper + `_execute_validate_run` branch in `execute_run`**

Add the helper near `_sha256` (around line 75):

```python
def _compute_snapshot_hash(
    *,
    workspace_id: uuid.UUID,
    changeset_id: uuid.UUID | None,
    file_count: int,
    cli_version: str,
    validator_engine: str,
    account_id: str,
) -> str:
    payload = f"{workspace_id}|{changeset_id}|{file_count}|{cli_version}|{validator_engine}|{account_id}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

Add a new `_execute_validate_run` branch above the existing assertion branch (around line 460):

```python
async def _execute_validate_run(
    db: AsyncSession,
    run: WorkspaceRun,
    tmp_dir: str,
    cmd_config: dict,
) -> WorkspaceRun:
    """suitecloud_validate run: seed creds, run subprocess, parse hits, persist."""
    from app.services.workspace.suitecloud_auth_seeder import seed_credentials_for_run
    from app.services.workspace.validate_parser import (
        PARSER_VERSION,
        parse_suitecloud_validate_output,
    )
    from app.models.connection import Connection
    from app.models.workspace import ValidationHit

    start_time = time.monotonic()
    file_count = await _materialize_snapshot(db, run, tmp_dir)

    # Resolve account_id for snapshot hash
    conn_row = (
        await db.execute(
            select(Connection).where(Connection.tenant_id == run.tenant_id, Connection.is_active.is_(True))
        )
    ).scalar_one_or_none()
    account_id = (conn_row.account_id if conn_row else "unknown") or "unknown"

    try:
        await seed_credentials_for_run(
            db=db,
            tenant_id=run.tenant_id,
            auth_root=Path(tmp_dir),
            project_id=str(run.workspace_id),
        )
    except Exception as exc:
        run.status = "failed"
        run.exit_code = -1
        run.gate_status = "block"
        run.validator_engine = "suitecloud_server"
        run.completed_at = datetime.now(timezone.utc)
        run.duration_ms = int((time.monotonic() - start_time) * 1000)
        await _store_artifact(db, run, "stderr", f"auth_required: {exc}")
        await _audit_run_event(db, run, action="run_failed", status="error", error_message=str(exc))
        await db.flush()
        return run

    # Run with HOME set to tmp_dir so the CLI finds our seeded creds.
    env_override = {"HOME": tmp_dir, "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}
    proc = await asyncio.create_subprocess_exec(
        *cmd_config["cmd"],
        cwd=tmp_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env_override,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=cmd_config["timeout"])
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        exit_code = proc.returncode or 0
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        run.status = "failed"
        run.exit_code = -2
        run.gate_status = "block"
        run.validator_engine = "suitecloud_server"
        run.completed_at = datetime.now(timezone.utc)
        run.duration_ms = int((time.monotonic() - start_time) * 1000)
        await _store_artifact(db, run, "stderr", "timeout")
        await _audit_run_event(db, run, action="run_failed", status="error", error_message="timeout")
        await db.flush()
        return run

    duration_ms = int((time.monotonic() - start_time) * 1000)
    parsed = parse_suitecloud_validate_output(stdout)

    # Persist artifacts (raw stdout/stderr always — codex #6 fallback)
    if stdout:
        await _store_artifact(db, run, "stdout", stdout)
    if stderr:
        await _store_artifact(db, run, "stderr", stderr)

    # Persist hits
    for parsed_hit in parsed.hits:
        db.add(
            ValidationHit(
                tenant_id=run.tenant_id,
                run_id=run.id,
                file_path=parsed_hit.file_path,
                line=parsed_hit.line,
                severity=parsed_hit.severity,
                code=parsed_hit.code,
                rule_id=None,  # populated by mechanical_fix_classifier in a later task
                message=parsed_hit.message,
                fingerprint=parsed_hit.fingerprint,
            )
        )

    # Run-record updates
    run.exit_code = exit_code
    run.has_errors = parsed.has_errors
    run.has_warnings = parsed.has_warnings
    run.parser_version = PARSER_VERSION
    run.validator_engine = "suitecloud_server"
    run.gate_status = "block" if parsed.has_errors else "pass"
    run.status = "failed" if parsed.has_errors else "passed"
    run.snapshot_hash = _compute_snapshot_hash(
        workspace_id=run.workspace_id,
        changeset_id=run.changeset_id,
        file_count=file_count,
        cli_version="suitecloud-cli@latest",  # constant for v1; reading the live `--version` is deferred to a follow-up so we don't shell-out twice per run
        validator_engine="suitecloud_server",
        account_id=account_id,
    )
    run.completed_at = datetime.now(timezone.utc)
    run.duration_ms = duration_ms

    await _store_artifact(
        db, run, "result_json",
        json.dumps(
            {
                "run_id": str(run.id),
                "run_type": run.run_type,
                "status": run.status,
                "gate_status": run.gate_status,
                "has_errors": run.has_errors,
                "has_warnings": run.has_warnings,
                "hit_count": len(parsed.hits),
                "duration_ms": duration_ms,
                "parser_version": PARSER_VERSION,
                "validator_engine": "suitecloud_server",
            },
            default=str,
        ),
    )
    await _audit_run_event(
        db,
        run,
        action="run_succeeded" if run.status == "passed" else "run_failed",
        status="success" if run.status == "passed" else "error",
        payload={"hit_count": len(parsed.hits), "gate_status": run.gate_status},
    )
    await db.flush()
    return run
```

Wire it in `execute_run` by adding a branch right before the existing `if run.run_type == "suiteql_assertions":` (around line 465):

```python
    if run.run_type == "suitecloud_validate":
        tmp_dir = tempfile.mkdtemp(prefix=f"workspace_run_{run.tenant_id}_")
        try:
            return await _execute_validate_run(db, run, tmp_dir, cmd_config)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && .venv/bin/python -m pytest tests/services/workspace/test_validate_runner_integration.py -v`
Expected: 3 PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/runner_service.py backend/tests/services/workspace/test_validate_runner_integration.py
git commit -m "feat(workspace): swap sdf_validate → suitecloud_validate in runner allowlist

Adds _execute_validate_run branch that seeds CLI credentials, runs
suitecloud project:validate --server, parses hits, and persists
ValidationHit rows + new run-record columns (validator_engine,
parser_version, has_errors, has_warnings, gate_status, snapshot_hash).
Removes sdf_validate entry — no silent fallback per codex #7."
```

---

### Task 5: Dockerfile.prod — install suitecloud CLI

**Files:**
- Modify: `backend/Dockerfile.prod`
- Test: manual deploy verification (no unit test possible)

- [ ] **Step 1: Add `npm install -g @oracle/suitecloud-cli` to runner image**

Edit `backend/Dockerfile.prod` — find the `RUN apt-get install` block (around line 22) and add Node + the CLI before non-root user creation:

```dockerfile
# Install Node.js 20 + suitecloud CLI for workspace validate/deploy.
# Pinned major version of the CLI so behavior changes don't surprise the runner.
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates gnupg && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    npm install -g @oracle/suitecloud-cli@^2 && \
    apt-get purge -y --auto-remove curl gnupg && \
    rm -rf /var/lib/apt/lists/* /root/.npm
```

- [ ] **Step 2: Local sanity check the image builds**

Run:
```bash
docker buildx build --platform linux/amd64 -t backend-validate-test -f backend/Dockerfile.prod . --target base 2>&1 | tail -20
```
Expected: build succeeds; final image contains `suitecloud --version` output.

- [ ] **Step 3: Add startup smoke check**

Add a sanity-check line near the bottom of the Dockerfile so a missing CLI fails the build (codex #14):
```dockerfile
RUN suitecloud --version
```

- [ ] **Step 4: Commit**

```bash
git add backend/Dockerfile.prod
git commit -m "feat(deploy): install @oracle/suitecloud-cli in backend image"
```

---

### Task 6: auto_validate_orchestrator

**Files:**
- Create: `backend/app/services/workspace/auto_validate_orchestrator.py`
- Create: `backend/tests/services/workspace/test_auto_validate_orchestrator.py`

- [ ] **Step 1: Write the failing orchestrator test**

```python
# backend/tests/services/workspace/test_auto_validate_orchestrator.py
"""Auto-validate orchestrator unit tests."""
from __future__ import annotations

import uuid

import pytest

from app.services.workspace.auto_validate_orchestrator import (
    LOOP_BUDGET,
    AutoValidateOrchestrator,
    LoopBudgetExceeded,
)


@pytest.mark.asyncio
async def test_debounce_cancels_superseded_run(monkeypatch) -> None:
    orchestrator = AutoValidateOrchestrator()
    workspace_id = uuid.uuid4()
    enqueued: list[uuid.UUID] = []

    async def fake_create_run(*, workspace_id: uuid.UUID, **_) -> uuid.UUID:
        run_id = uuid.uuid4()
        enqueued.append(run_id)
        return run_id

    orchestrator._create_run = fake_create_run

    first = await orchestrator.enqueue(workspace_id=workspace_id, changeset_id=uuid.uuid4(), tenant_id=uuid.uuid4(), triggered_by=uuid.uuid4())
    second = await orchestrator.enqueue(workspace_id=workspace_id, changeset_id=uuid.uuid4(), tenant_id=uuid.uuid4(), triggered_by=uuid.uuid4())

    assert orchestrator.is_cancelled(first) is True
    assert orchestrator.is_cancelled(second) is False


@pytest.mark.asyncio
async def test_loop_budget_blocks_after_n_auto_fixes() -> None:
    orchestrator = AutoValidateOrchestrator()
    changeset_id = uuid.uuid4()

    for _ in range(LOOP_BUDGET):
        orchestrator.record_auto_fix(changeset_id)

    assert orchestrator.under_budget(changeset_id) is False
    with pytest.raises(LoopBudgetExceeded):
        orchestrator.assert_under_budget(changeset_id)


def test_fingerprint_dedup_blocks_repeat_auto_propose() -> None:
    orchestrator = AutoValidateOrchestrator()
    changeset_id = uuid.uuid4()
    fp = "a" * 64

    assert orchestrator.should_auto_propose(changeset_id, fp) is True
    orchestrator.record_auto_propose(changeset_id, fp)
    assert orchestrator.should_auto_propose(changeset_id, fp) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/services/workspace/test_auto_validate_orchestrator.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement the orchestrator**

```python
# backend/app/services/workspace/auto_validate_orchestrator.py
"""Auto-validate orchestrator: debounce, loop budget, fingerprint dedup.

Three responsibilities, one class, no I/O:
1. Debounce: when a workspace gets multiple apply_patch events in quick succession,
   only the most recent enqueued validate run actually executes; superseded ones
   are marked cancelled (the runner skips cancelled runs at execute_run entry).
2. Loop budget: per changeset, the agent gets at most LOOP_BUDGET auto-fix
   rounds before further auto-propose attempts are refused (the agent must
   narrate-only beyond that).
3. Fingerprint dedup: the same finding fingerprint cannot trigger an auto-propose
   twice within the same changeset.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Awaitable, Callable

LOOP_BUDGET = 3
DEBOUNCE_SECONDS = 2.0


class LoopBudgetExceeded(Exception):
    """Raised when assert_under_budget is called past LOOP_BUDGET auto-fixes."""


class AutoValidateOrchestrator:
    """Per-process state. Single instance reused across the FastAPI app."""

    def __init__(self) -> None:
        self._latest_run_per_workspace: dict[uuid.UUID, uuid.UUID] = {}
        self._cancelled: set[uuid.UUID] = set()
        self._auto_fix_count: dict[uuid.UUID, int] = defaultdict(int)
        self._proposed_fingerprints: dict[uuid.UUID, set[str]] = defaultdict(set)
        # Set by the FastAPI lifespan to the runner_service.create_run coroutine.
        self._create_run: Callable[..., Awaitable[uuid.UUID]] | None = None

    async def enqueue(
        self,
        *,
        workspace_id: uuid.UUID,
        changeset_id: uuid.UUID,
        tenant_id: uuid.UUID,
        triggered_by: uuid.UUID,
    ) -> uuid.UUID:
        """Enqueue a validate run; cancel any in-flight queued run for the same workspace."""
        if self._create_run is None:
            raise RuntimeError("AutoValidateOrchestrator not initialized: _create_run is None")

        previous = self._latest_run_per_workspace.get(workspace_id)
        if previous is not None:
            self._cancelled.add(previous)

        run_id = await self._create_run(
            workspace_id=workspace_id,
            changeset_id=changeset_id,
            tenant_id=tenant_id,
            triggered_by=triggered_by,
            run_type="suitecloud_validate",
        )
        self._latest_run_per_workspace[workspace_id] = run_id
        return run_id

    def is_cancelled(self, run_id: uuid.UUID) -> bool:
        return run_id in self._cancelled

    def under_budget(self, changeset_id: uuid.UUID) -> bool:
        return self._auto_fix_count[changeset_id] < LOOP_BUDGET

    def assert_under_budget(self, changeset_id: uuid.UUID) -> None:
        if not self.under_budget(changeset_id):
            raise LoopBudgetExceeded(f"changeset {changeset_id} exceeded {LOOP_BUDGET} auto-fix rounds")

    def record_auto_fix(self, changeset_id: uuid.UUID) -> None:
        self._auto_fix_count[changeset_id] += 1

    def should_auto_propose(self, changeset_id: uuid.UUID, fingerprint: str) -> bool:
        return fingerprint not in self._proposed_fingerprints[changeset_id]

    def record_auto_propose(self, changeset_id: uuid.UUID, fingerprint: str) -> None:
        self._proposed_fingerprints[changeset_id].add(fingerprint)


_INSTANCE: AutoValidateOrchestrator | None = None


def get_orchestrator() -> AutoValidateOrchestrator:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = AutoValidateOrchestrator()
    return _INSTANCE
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/python -m pytest tests/services/workspace/test_auto_validate_orchestrator.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/workspace/auto_validate_orchestrator.py backend/tests/services/workspace/test_auto_validate_orchestrator.py
git commit -m "feat(workspace): auto-validate orchestrator (debounce + loop budget + fingerprint dedup)"
```

---

### Task 7: mechanical_fix_classifier

**Files:**
- Create: `backend/app/services/workspace/mechanical_fix_classifier.py`
- Create: `backend/tests/services/workspace/test_mechanical_fix_classifier.py`

- [ ] **Step 1: Write the failing classifier test**

```python
# backend/tests/services/workspace/test_mechanical_fix_classifier.py
"""Mechanical-fix classifier tests — deny-by-default."""
from __future__ import annotations

import pytest

from app.services.workspace.mechanical_fix_classifier import (
    MechanicalFix,
    classify,
)


def test_unknown_code_returns_none() -> None:
    assert classify(code="MADE-UP-RULE", message="hi", file_path="x.js", line=1) is None


def test_owasp_a03_is_narrate_only() -> None:
    """OWASP severity is judgment — never auto-fix."""
    result = classify(code="OWASP-A03", message="injection", file_path="x.js", line=42)
    assert result is None


def test_deprecated_2x_api_is_fixable() -> None:
    """nlapiSearchRecord → N/search migration is deterministic."""
    result = classify(
        code="SUITESCRIPT-DEPRECATED-2X",
        message="nlapiSearchRecord is deprecated in 2.1; use N/search.",
        file_path="src/UserEvents/auditLog.js",
        line=18,
    )
    assert isinstance(result, MechanicalFix)
    assert result.rule_id == "netsuite-suitescript-upgrade/nlapi-to-n-search"
    assert result.replacement_summary  # non-empty


def test_governance_check_is_narrate_only() -> None:
    """remainingUsage check inside loop is judgment-call (where to put it)."""
    result = classify(
        code="GOVERNANCE-CHECK",
        message="Missing remainingUsage check inside loop.",
        file_path="x.js",
        line=34,
    )
    assert result is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/services/workspace/test_mechanical_fix_classifier.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement the classifier**

```python
# backend/app/services/workspace/mechanical_fix_classifier.py
"""Deny-by-default mechanical-fix classifier.

Maps Oracle validate codes → deterministic patch generators. ONLY rules in
the allowlist below are auto-fixable; everything else (including OWASP
severity, architectural concerns, governance hot-spots) is narrate-only.

To add a new fixable rule:
1. Append a row to _ALLOWED_RULES with the Oracle code, rule_id (RAG
   citation key), and a `replacement_summary` describing the deterministic
   transform.
2. Wire the actual patch generator in workspace_propose_patch dispatch
   (keyed on rule_id).

Codex #10 explicitly demands deny-by-default — DO NOT add general regex
matching here. Each rule is opt-in.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MechanicalFix:
    rule_id: str
    replacement_summary: str


_ALLOWED_RULES: dict[str, MechanicalFix] = {
    "SUITESCRIPT-DEPRECATED-2X": MechanicalFix(
        rule_id="netsuite-suitescript-upgrade/nlapi-to-n-search",
        replacement_summary="Replace nlapi* call with the equivalent N/search / N/record API per Oracle migration table.",
    ),
    # Future fixable rules go here. Each must have a deterministic transform
    # (no judgment call, no business-logic guess). Validate by:
    # - "Given this snippet, do all reasonable engineers produce the same fix?"
    # - "Does the transform require knowing user-specific config?"
    # If either fails — keep the rule narrate-only.
}


def classify(*, code: str | None, message: str, file_path: str | None, line: int | None) -> MechanicalFix | None:
    """Return a MechanicalFix iff the rule is in the allowlist; None otherwise."""
    if not code:
        return None
    return _ALLOWED_RULES.get(code)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/python -m pytest tests/services/workspace/test_mechanical_fix_classifier.py -v`
Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/workspace/mechanical_fix_classifier.py backend/tests/services/workspace/test_mechanical_fix_classifier.py
git commit -m "feat(workspace): deny-by-default mechanical-fix classifier"
```

---

### Task 8: Wire workspace_apply_patch → orchestrator

**Files:**
- Modify: `backend/app/mcp/tools/workspace_tools.py` (around `workspace_apply_patch` execute fn)
- Modify: `backend/tests/mcp/tools/test_workspace_apply_patch.py` (or create if absent)

- [ ] **Step 1: Write the failing wiring test**

```python
# backend/tests/mcp/tools/test_workspace_apply_patch.py
"""workspace_apply_patch should enqueue an auto-validate run on success."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.mcp.tools import workspace_tools


@pytest.mark.asyncio
async def test_apply_patch_success_enqueues_validate(seeded_workspace_with_changeset, db) -> None:
    enqueue_mock = AsyncMock(return_value=uuid.uuid4())
    with patch(
        "app.services.workspace.auto_validate_orchestrator.get_orchestrator"
    ) as get_orchestrator_mock:
        get_orchestrator_mock.return_value.enqueue = enqueue_mock

        result = await workspace_tools.workspace_apply_patch(
            params={"changeset_id": str(seeded_workspace_with_changeset.changeset_id)},
            context={
                "tenant_id": str(seeded_workspace_with_changeset.tenant_id),
                "user_id": str(seeded_workspace_with_changeset.created_by),
                "db": db,
            },
        )

    assert result["status"] in ("ok", "applied")
    enqueue_mock.assert_awaited_once()
    args = enqueue_mock.call_args.kwargs
    assert args["workspace_id"] == seeded_workspace_with_changeset.id


@pytest.mark.asyncio
async def test_apply_patch_failure_does_not_enqueue(seeded_workspace_with_invalid_changeset, db) -> None:
    enqueue_mock = AsyncMock()
    with patch(
        "app.services.workspace.auto_validate_orchestrator.get_orchestrator"
    ) as get_orchestrator_mock:
        get_orchestrator_mock.return_value.enqueue = enqueue_mock

        result = await workspace_tools.workspace_apply_patch(
            params={"changeset_id": str(seeded_workspace_with_invalid_changeset.changeset_id)},
            context={
                "tenant_id": str(seeded_workspace_with_invalid_changeset.tenant_id),
                "user_id": str(seeded_workspace_with_invalid_changeset.created_by),
                "db": db,
            },
        )

    assert result["status"] == "error"
    enqueue_mock.assert_not_awaited()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/mcp/tools/test_workspace_apply_patch.py -v`
Expected: FAIL — `apply_patch` does not enqueue.

- [ ] **Step 3: Wire the orchestrator into `workspace_apply_patch`**

Edit `backend/app/mcp/tools/workspace_tools.py`. Find the `workspace_apply_patch` execute function and add the enqueue call after the changeset is successfully applied (do NOT enqueue on validation/auth failures):

```python
# At the top of workspace_tools.py
from app.services.workspace.auto_validate_orchestrator import get_orchestrator

# Inside workspace_apply_patch's execute path, AFTER the changeset has been
# successfully marked "applied" and `db.flush()` has run:
try:
    await get_orchestrator().enqueue(
        workspace_id=changeset.workspace_id,
        changeset_id=changeset.id,
        tenant_id=tenant_id,
        triggered_by=user_id,
    )
except Exception as exc:
    # Don't fail the apply just because the queue failed; log + audit.
    logger.warning("workspace.apply_patch.enqueue_failed", changeset_id=str(changeset.id), error=str(exc))
```

Initialize `_create_run` on app startup (`backend/app/main.py` lifespan or a `core/lifespan.py` equivalent — find the existing startup hook):

```python
from app.services import runner_service
from app.services.workspace.auto_validate_orchestrator import get_orchestrator

# Inside the FastAPI lifespan startup branch:
get_orchestrator()._create_run = lambda **kwargs: runner_service.create_run(db=None, **kwargs)
# NOTE: the orchestrator needs a fresh DB session per call; refactor this
# closure to acquire one from `async_session_factory()`. Implementation:

async def _enqueue_with_session(**kwargs) -> uuid.UUID:
    from app.core.database import async_session_factory
    async with async_session_factory() as session:
        run = await runner_service.create_run(db=session, **kwargs)
        await session.commit()
        return run.id

get_orchestrator()._create_run = _enqueue_with_session
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/python -m pytest tests/mcp/tools/test_workspace_apply_patch.py -v`
Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/mcp/tools/workspace_tools.py backend/app/main.py backend/tests/mcp/tools/test_workspace_apply_patch.py
git commit -m "feat(workspace): auto-enqueue suitecloud_validate on apply_patch success"
```

---

### Task 9: deploy_service snapshot-hash freshness

**Files:**
- Modify: `backend/app/services/deploy_service.py` (entire `check_deploy_prerequisites` + helpers)
- Create: `backend/tests/services/workspace/test_deploy_freshness.py`

- [ ] **Step 1: Write the failing freshness tests**

```python
# backend/tests/services/workspace/test_deploy_freshness.py
"""Deploy gate freshness: snapshot-hash lookup + suitecloud_validate switch."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.workspace import WorkspaceRun
from app.services import deploy_service


@pytest.mark.asyncio
async def test_deploy_uses_suitecloud_validate_run_type(seeded_workspace_with_changeset, db) -> None:
    """sdf_validate is gone; only suitecloud_validate counts."""
    run = WorkspaceRun(
        tenant_id=seeded_workspace_with_changeset.tenant_id,
        workspace_id=seeded_workspace_with_changeset.id,
        changeset_id=seeded_workspace_with_changeset.changeset_id,
        run_type="sdf_validate",  # legacy
        status="passed",
        triggered_by=seeded_workspace_with_changeset.created_by,
    )
    db.add(run)
    await db.flush()

    gates = await deploy_service.check_deploy_prerequisites(
        db=db,
        changeset_id=seeded_workspace_with_changeset.changeset_id,
        tenant_id=seeded_workspace_with_changeset.tenant_id,
    )
    # Legacy sdf_validate row must NOT count as a passing validate.
    assert gates["gates"]["validate"]["status"] == "missing"


@pytest.mark.asyncio
async def test_fresh_validate_with_matching_hash_passes_gate(seeded_workspace_with_changeset, db) -> None:
    run = WorkspaceRun(
        tenant_id=seeded_workspace_with_changeset.tenant_id,
        workspace_id=seeded_workspace_with_changeset.id,
        changeset_id=seeded_workspace_with_changeset.changeset_id,
        run_type="suitecloud_validate",
        status="passed",
        gate_status="pass",
        snapshot_hash="a" * 64,
        triggered_by=seeded_workspace_with_changeset.created_by,
    )
    db.add(run)
    await db.flush()

    gates = await deploy_service.check_deploy_prerequisites(
        db=db,
        changeset_id=seeded_workspace_with_changeset.changeset_id,
        tenant_id=seeded_workspace_with_changeset.tenant_id,
        expected_snapshot_hash="a" * 64,
    )
    assert gates["gates"]["validate"]["status"] == "passed"
    assert gates["gates"]["validate"]["fresh"] is True


@pytest.mark.asyncio
async def test_stale_snapshot_hash_marks_validate_stale(seeded_workspace_with_changeset, db) -> None:
    run = WorkspaceRun(
        tenant_id=seeded_workspace_with_changeset.tenant_id,
        workspace_id=seeded_workspace_with_changeset.id,
        changeset_id=seeded_workspace_with_changeset.changeset_id,
        run_type="suitecloud_validate",
        status="passed",
        gate_status="pass",
        snapshot_hash="a" * 64,
        triggered_by=seeded_workspace_with_changeset.created_by,
    )
    db.add(run)
    await db.flush()

    gates = await deploy_service.check_deploy_prerequisites(
        db=db,
        changeset_id=seeded_workspace_with_changeset.changeset_id,
        tenant_id=seeded_workspace_with_changeset.tenant_id,
        expected_snapshot_hash="b" * 64,  # mismatch
    )
    assert gates["gates"]["validate"]["status"] == "stale"
    assert gates["allowed"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/python -m pytest tests/services/workspace/test_deploy_freshness.py -v`
Expected: 3 FAIL.

- [ ] **Step 3: Update `check_deploy_prerequisites` in `deploy_service.py`**

Replace the validate-gate block (lines 53-58 in the existing file) with:

```python
    # Check validate (suitecloud_validate only — legacy sdf_validate ignored)
    validate_run = await _get_latest_run(db, changeset_id, tenant_id, "suitecloud_validate")
    if validate_run is None:
        gates["validate"] = {"status": "missing", "run_id": None, "fresh": False}
    else:
        # If caller passed an expected_snapshot_hash, compare for freshness.
        is_fresh = (
            expected_snapshot_hash is None
            or validate_run.snapshot_hash == expected_snapshot_hash
        )
        if not is_fresh:
            gates["validate"] = {
                "status": "stale",
                "run_id": str(validate_run.id),
                "fresh": False,
            }
        elif validate_run.gate_status == "pass":
            gates["validate"] = {"status": "passed", "run_id": str(validate_run.id), "fresh": True}
        else:
            gates["validate"] = {
                "status": validate_run.status,
                "run_id": str(validate_run.id),
                "fresh": True,
            }
```

Add `expected_snapshot_hash` parameter to `check_deploy_prerequisites` (default `None`):

```python
async def check_deploy_prerequisites(
    db: AsyncSession,
    changeset_id: uuid.UUID,
    tenant_id: uuid.UUID,
    require_assertions: bool = False,
    override_reason: str | None = None,
    expected_snapshot_hash: str | None = None,
) -> dict[str, Any]:
```

Update the `validate_ok` evaluation (line 82) to be:

```python
    validate_ok = gates["validate"]["status"] == "passed" and gates["validate"]["fresh"] is True
```

Update `get_latest_runs_for_changeset` (line 151) to use the new run_type:

```python
    run_types = ["suitecloud_validate", "jest_unit_test", "suiteql_assertions", "deploy_sandbox"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/python -m pytest tests/services/workspace/test_deploy_freshness.py tests/services/workspace -v`
Expected: 3 PASS + no regressions in the broader workspace test set.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/deploy_service.py backend/tests/services/workspace/test_deploy_freshness.py
git commit -m "feat(workspace): deploy gate uses suitecloud_validate + snapshot-hash freshness"
```

---

### Task 10: Workspace agent narration + auto-propose

**Files:**
- Modify: `backend/app/services/chat/agents/workspace_agent.py`
- Create: `backend/tests/services/chat/agents/test_workspace_agent_validate_narration.py`

- [ ] **Step 1: Write the failing narration test**

```python
# backend/tests/services/chat/agents/test_workspace_agent_validate_narration.py
"""Workspace agent post-validate narration + auto-propose."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.models.workspace import ValidationHit
from app.services.chat.agents.workspace_agent import (
    WorkspaceAgent,
    _batch_hits_by_family,
    _maybe_auto_propose_fix,
)


def test_batches_hits_by_code_family() -> None:
    hits = [
        ValidationHit(file_path="x.js", line=1, severity="error", code="OWASP-A03", message="a", fingerprint="1" * 64, run_id=uuid.uuid4(), tenant_id=uuid.uuid4()),
        ValidationHit(file_path="x.js", line=2, severity="error", code="OWASP-A03", message="b", fingerprint="2" * 64, run_id=uuid.uuid4(), tenant_id=uuid.uuid4()),
        ValidationHit(file_path="x.js", line=3, severity="warning", code="SUITESCRIPT-DEPRECATED-2X", message="c", fingerprint="3" * 64, run_id=uuid.uuid4(), tenant_id=uuid.uuid4()),
    ]
    families = _batch_hits_by_family(hits)
    assert set(families.keys()) == {"OWASP-A03", "SUITESCRIPT-DEPRECATED-2X"}
    assert len(families["OWASP-A03"]) == 2
    assert len(families["SUITESCRIPT-DEPRECATED-2X"]) == 1


@pytest.mark.asyncio
async def test_auto_propose_called_only_for_fixable_codes() -> None:
    propose_mock = AsyncMock(return_value={"changeset_id": str(uuid.uuid4())})
    fixable_hit = ValidationHit(
        file_path="x.js", line=18, severity="warning",
        code="SUITESCRIPT-DEPRECATED-2X",
        message="nlapi deprecated", fingerprint="a" * 64,
        run_id=uuid.uuid4(), tenant_id=uuid.uuid4(),
    )
    with patch(
        "app.services.workspace.auto_validate_orchestrator.get_orchestrator"
    ) as orch_mock, patch(
        "app.mcp.tools.workspace_tools.workspace_propose_patch", new=propose_mock
    ):
        orch_mock.return_value.under_budget.return_value = True
        orch_mock.return_value.should_auto_propose.return_value = True

        await _maybe_auto_propose_fix(
            hit=fixable_hit,
            changeset_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
        )
    propose_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_auto_propose_skipped_for_owasp() -> None:
    propose_mock = AsyncMock()
    owasp_hit = ValidationHit(
        file_path="x.js", line=42, severity="error",
        code="OWASP-A03",
        message="injection", fingerprint="b" * 64,
        run_id=uuid.uuid4(), tenant_id=uuid.uuid4(),
    )
    with patch(
        "app.services.workspace.auto_validate_orchestrator.get_orchestrator"
    ) as orch_mock, patch(
        "app.mcp.tools.workspace_tools.workspace_propose_patch", new=propose_mock
    ):
        orch_mock.return_value.under_budget.return_value = True
        orch_mock.return_value.should_auto_propose.return_value = True

        await _maybe_auto_propose_fix(
            hit=owasp_hit,
            changeset_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
        )
    propose_mock.assert_not_awaited()


def test_workspace_agent_allows_run_validate_tool() -> None:
    agent = WorkspaceAgent(tenant_id=uuid.uuid4(), user_id=uuid.uuid4(), correlation_id="t")
    tool_names = {t["name"] for t in agent.tool_definitions}
    assert "workspace_run_validate" in tool_names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/services/chat/agents/test_workspace_agent_validate_narration.py -v`
Expected: 4 FAIL — helpers don't exist; `workspace_run_validate` not in allowed tools.

- [ ] **Step 3: Update `workspace_agent.py`**

Add `workspace_run_validate` to the allowed tools set and add narration helpers:

```python
# Replace _WORKSPACE_TOOL_NAMES (line 16):
_WORKSPACE_TOOL_NAMES = frozenset(
    {
        "workspace_list_files",
        "workspace_read_file",
        "workspace_search",
        "workspace_propose_patch",
        "workspace_run_validate",  # NEW: agent can call validate explicitly
        "rag_search",
        "tenant_save_learned_rule",
    }
)
```

Append helper functions to the bottom of the file:

```python
import uuid as _uuid
from collections import defaultdict
from typing import Iterable

from app.models.workspace import ValidationHit
from app.services.workspace.auto_validate_orchestrator import get_orchestrator
from app.services.workspace.mechanical_fix_classifier import classify

_HIT_FAMILY_CITATION_CAP = 3


def _batch_hits_by_family(hits: Iterable[ValidationHit]) -> dict[str, list[ValidationHit]]:
    """Group hits by code so the agent narrates one citation per family (codex #8)."""
    families: dict[str, list[ValidationHit]] = defaultdict(list)
    for hit in hits:
        key = hit.code or "UNCODED"
        families[key].append(hit)
    return dict(families)


async def _maybe_auto_propose_fix(
    *,
    hit: ValidationHit,
    changeset_id: _uuid.UUID,
    tenant_id: _uuid.UUID,
    user_id: _uuid.UUID,
) -> None:
    """If the hit's code is in the mechanical-fix allowlist AND we're under budget,
    enqueue a draft fix patch via workspace_propose_patch.

    Codex #10: deny-by-default. The classifier is the only gate.
    """
    fix = classify(code=hit.code, message=hit.message, file_path=hit.file_path, line=hit.line)
    if fix is None:
        return

    orch = get_orchestrator()
    if not orch.under_budget(changeset_id):
        return
    if not orch.should_auto_propose(changeset_id, hit.fingerprint):
        return

    from app.mcp.tools import workspace_tools  # local import to avoid cycle

    await workspace_tools.workspace_propose_patch(
        params={
            "title": f"Auto-fix: {fix.replacement_summary}",
            "rule_id": fix.rule_id,
            "target_file": hit.file_path,
            "target_line": hit.line,
        },
        context={"tenant_id": str(tenant_id), "user_id": str(user_id)},
    )
    orch.record_auto_propose(changeset_id, hit.fingerprint)
    orch.record_auto_fix(changeset_id)
```

Extend `_SYSTEM_PROMPT` with a new `<post_validate_workflow>` block (insert before `<output_instructions>`):

```python
<post_validate_workflow>
WHEN VALIDATE RESULTS ARE INJECTED INTO THE CONVERSATION:
1. The system has already grouped hits by code family. ONE narration per family.
2. For EACH family: pull a citation from the appropriate oracle/* RAG partition
   (ai-connector, owasp, sdf-docs, sdf-roles, records, upgrade, uif-spa) using
   rag_search. Cite the partition + chunk in the narration.
3. Limit narration to %d families MAX. If more families, say "X additional
   warnings — see the runs panel for details" rather than narrating all.
4. The system has already auto-proposed fixes for mechanically-fixable hits. Do
   NOT propose fixes manually for OWASP, governance, or architectural hits —
   narrate only and let the user decide.
</post_validate_workflow>
""" % _HIT_FAMILY_CITATION_CAP
```

(Adjust the existing string concatenation accordingly — keep the trailing triple-quote.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/python -m pytest tests/services/chat/agents/test_workspace_agent_validate_narration.py -v`
Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/chat/agents/workspace_agent.py backend/tests/services/chat/agents/test_workspace_agent_validate_narration.py
git commit -m "feat(workspace): post-validate narration + auto-propose for fixable hits"
```

---

### Task 11: Frontend hits-table + retry button

**Files:**
- Modify: `frontend/src/lib/types.ts`
- Modify: `frontend/src/components/workspace/runs-panel.tsx`
- Create: `frontend/src/components/workspace/validation-hits-table.tsx`
- Create: `frontend/src/components/workspace/__tests__/validation-hits-table.test.tsx`

- [ ] **Step 1: Add types**

Edit `frontend/src/lib/types.ts` and append:

```typescript
export type ValidatorEngine = "suitecloud_server" | "sdf_legacy" | null;
export type RunGateStatus = "pass" | "block" | "stale" | "unknown" | null;

export interface ValidationHit {
  id: string;
  run_id: string;
  file_path: string | null;
  line: number | null;
  severity: "error" | "warning" | "info" | "parser_error";
  code: string | null;
  rule_id: string | null;
  message: string;
  fingerprint: string;
}

// Extend WorkspaceRun (find the existing interface and add):
//   validator_engine?: ValidatorEngine;
//   has_errors?: boolean;
//   has_warnings?: boolean;
//   gate_status?: RunGateStatus;
//   snapshot_hash?: string | null;
//   parser_version?: string | null;
//   findings?: ValidationHit[]; // populated when fetching a single run
```

- [ ] **Step 2: Write the failing component test**

```typescript
// frontend/src/components/workspace/__tests__/validation-hits-table.test.tsx
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { ValidationHitsTable } from "../validation-hits-table";
import type { ValidationHit } from "@/lib/types";

const hits: ValidationHit[] = [
  {
    id: "h1",
    run_id: "r1",
    file_path: "src/Suitelets/foo.js",
    line: 42,
    severity: "error",
    code: "OWASP-A03",
    rule_id: null,
    message: "Unsanitized user input flowed into N/query",
    fingerprint: "f1",
  },
  {
    id: "h2",
    run_id: "r1",
    file_path: "src/Suitelets/foo.js",
    line: 67,
    severity: "warning",
    code: "SUITESCRIPT-DEPRECATED-2X",
    rule_id: null,
    message: "nlapi deprecated",
    fingerprint: "f2",
  },
];

describe("ValidationHitsTable", () => {
  it("renders one row per hit with file:line + severity badge", () => {
    render(<ValidationHitsTable hits={hits} />);
    expect(screen.getByText("src/Suitelets/foo.js:42")).toBeInTheDocument();
    expect(screen.getByText("OWASP-A03")).toBeInTheDocument();
    expect(screen.getByText("Unsanitized user input flowed into N/query")).toBeInTheDocument();
    expect(screen.getAllByTestId("severity-badge")).toHaveLength(2);
  });

  it("shows empty state when no hits", () => {
    render(<ValidationHitsTable hits={[]} />);
    expect(screen.getByText(/no validate hits/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/components/workspace/__tests__/validation-hits-table.test.tsx`
Expected: FAIL — component doesn't exist.

- [ ] **Step 4: Implement the component**

```typescript
// frontend/src/components/workspace/validation-hits-table.tsx
"use client";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { ValidationHit } from "@/lib/types";

interface Props {
  hits: ValidationHit[];
}

const severityStyles: Record<ValidationHit["severity"], string> = {
  error: "bg-red-100 text-red-700",
  warning: "bg-amber-100 text-amber-700",
  info: "bg-gray-100 text-gray-700",
  parser_error: "bg-orange-100 text-orange-700",
};

function formatLocation(file: string | null, line: number | null): string {
  if (!file) return "—";
  return line ? `${file}:${line}` : file;
}

export function ValidationHitsTable({ hits }: Props) {
  if (hits.length === 0) {
    return (
      <p className="text-[11px] italic text-muted-foreground">
        No validate hits
      </p>
    );
  }

  return (
    <div className="space-y-1">
      {hits.map((hit) => (
        <div
          key={hit.id}
          className="grid grid-cols-[max-content_max-content_max-content_1fr] items-start gap-2 rounded border bg-card px-2 py-1.5 text-[11px]"
        >
          <Badge
            data-testid="severity-badge"
            variant="secondary"
            className={cn("text-[10px]", severityStyles[hit.severity])}
          >
            {hit.severity}
          </Badge>
          <span className="font-mono text-muted-foreground">
            {formatLocation(hit.file_path, hit.line)}
          </span>
          <span className="font-mono">{hit.code ?? "—"}</span>
          <span className="text-foreground">{hit.message}</span>
        </div>
      ))}
    </div>
  );
}
```

- [ ] **Step 5: Wire the table into `runs-panel.tsx`**

Edit `frontend/src/components/workspace/runs-panel.tsx`. Add `suitecloud_validate` to the labels map (line 22):

```typescript
const runTypeLabels: Record<string, string> = {
  suitecloud_validate: "SuiteCloud Validate",
  jest_unit_test: "Jest Tests",
  suiteql_assertions: "SuiteQL Assertions",
  deploy_sandbox: "Sandbox Deploy",
};
```

Update `RunDetail` to render the hits table for `suitecloud_validate` runs (replace the existing `RunDetail` body):

```typescript
function RunDetail({ run }: { run: WorkspaceRun }) {
  const { data: artifacts = [] } = useRunArtifacts(run.id);
  const isValidate = run.run_type === "suitecloud_validate";
  const hits = run.findings ?? [];

  return (
    <div className="space-y-3">
      {isValidate && <ValidationHitsTable hits={hits} />}
      {(run.status === "failed" || run.gate_status === "stale") && isValidate && (
        <button
          onClick={() => retryValidate(run.id)}
          className="text-[11px] text-blue-600 underline hover:text-blue-700"
        >
          Retry validate
        </button>
      )}
      {artifacts.length > 0 && (
        <div className="space-y-2">
          {artifacts.map((a) => (
            <div key={a.id}>
              <p className="text-[10px] font-medium uppercase text-muted-foreground mb-0.5">
                {a.artifact_type}
              </p>
              <pre className="max-h-[200px] overflow-auto rounded bg-muted/50 p-2 text-[11px] font-mono whitespace-pre-wrap break-all">
                {a.content || "(empty)"}
              </pre>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
```

Replace the call site (around line 102-105) to pass `run` instead of just `runId`:

```typescript
{expandedId === run.id && (
  <div className="border-t px-3 py-2">
    <RunDetail run={run} />
  </div>
)}
```

Add `useRetryValidate` mutation hook in `frontend/src/hooks/use-runs.ts`:

```typescript
export function useRetryValidate() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (runId: string) =>
      apiClient.post<WorkspaceRun>(`/api/v1/workspaces/runs/${runId}/retry`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspace-runs"] });
    },
  });
}
```

(The `runs-panel.tsx` should call `useRetryValidate()` and pass the resulting `mutate` function as `retryValidate` — wire this in the parent component.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd frontend && npx vitest run src/components/workspace/__tests__/`
Expected: 2 PASS.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/lib/types.ts frontend/src/components/workspace/validation-hits-table.tsx frontend/src/components/workspace/__tests__/validation-hits-table.test.tsx frontend/src/components/workspace/runs-panel.tsx frontend/src/hooks/use-runs.ts
git commit -m "feat(workspace): validation hits table + retry button + types"
```

---

### Task 12: Integration test — full auto-validate loop

**Files:**
- Create: `backend/tests/integration/test_workspace_validate_e2e.py`

This test exercises the full chain: `workspace_apply_patch` → orchestrator enqueue → runner with mocked subprocess → parser → ValidationHit rows → agent narration helper → mechanical fix classifier → workspace_propose_patch.

- [ ] **Step 1: Write the integration test**

```python
# backend/tests/integration/test_workspace_validate_e2e.py
"""End-to-end: apply_patch → auto-validate → narration → auto-propose fix."""
from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.mcp.tools import workspace_tools
from app.models.workspace import ValidationHit, WorkspaceRun
from app.services import runner_service
from app.services.workspace.auto_validate_orchestrator import get_orchestrator


@pytest.mark.asyncio
async def test_apply_patch_triggers_validate_and_auto_propose_fix(
    seeded_workspace_with_changeset, db
) -> None:
    """Single happy-path E2E."""
    fixture = Path("tests/services/workspace/fixtures/suitecloud_validate_warnings.txt").read_text()

    # Wire orchestrator to runner_service for this test
    orch = get_orchestrator()
    orch._cancelled.clear()
    orch._latest_run_per_workspace.clear()
    orch._auto_fix_count.clear()
    orch._proposed_fingerprints.clear()

    async def _create_run_with_session(**kwargs) -> uuid.UUID:
        run = await runner_service.create_run(db=db, **kwargs)
        return run.id

    orch._create_run = _create_run_with_session

    propose_mock = AsyncMock(return_value={"changeset_id": str(uuid.uuid4())})

    with (
        patch("app.services.runner_service._run_subprocess", new=AsyncMock(return_value=(0, fixture, ""))),
        patch("app.services.workspace.suitecloud_auth_seeder.seed_credentials_for_run", new=AsyncMock(return_value=Path("/tmp/fake.json"))),
        patch("app.mcp.tools.workspace_tools.workspace_propose_patch", new=propose_mock),
    ):
        # 1. Agent applies a patch
        result = await workspace_tools.workspace_apply_patch(
            params={"changeset_id": str(seeded_workspace_with_changeset.changeset_id)},
            context={
                "tenant_id": str(seeded_workspace_with_changeset.tenant_id),
                "user_id": str(seeded_workspace_with_changeset.created_by),
                "db": db,
            },
        )
        assert result["status"] in ("ok", "applied")

        # 2. Validate run is enqueued and executed
        runs = (
            await db.execute(
                select(WorkspaceRun).where(
                    WorkspaceRun.workspace_id == seeded_workspace_with_changeset.id,
                    WorkspaceRun.run_type == "suitecloud_validate",
                )
            )
        ).scalars().all()
        assert len(runs) == 1
        run = runs[0]

        await runner_service.execute_run(db=db, run_id=run.id, tenant_id=run.tenant_id)

        # 3. ValidationHit rows persisted
        hits = (await db.execute(select(ValidationHit).where(ValidationHit.run_id == run.id))).scalars().all()
        assert len(hits) == 2  # warnings fixture has 2 warnings
        assert all(h.severity == "warning" for h in hits)

        # 4. Run-record gate_status reflects warnings-only (pass)
        await db.refresh(run)
        assert run.gate_status == "pass"
        assert run.has_warnings is True
        assert run.has_errors is False

        # 5. Mechanical-fix classifier auto-proposes for SUITESCRIPT-DEPRECATED-2X
        from app.services.chat.agents.workspace_agent import _maybe_auto_propose_fix

        for hit in hits:
            await _maybe_auto_propose_fix(
                hit=hit,
                changeset_id=run.changeset_id,
                tenant_id=run.tenant_id,
                user_id=seeded_workspace_with_changeset.created_by,
            )

        # The warnings fixture has 2 deprecated-2x hits → 2 auto-proposes (one per hit, until loop budget)
        assert propose_mock.await_count == 2
```

- [ ] **Step 2: Run the integration test**

Run: `cd backend && .venv/bin/python -m pytest tests/integration/test_workspace_validate_e2e.py -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add backend/tests/integration/test_workspace_validate_e2e.py
git commit -m "test(workspace): integration test for full auto-validate + auto-propose loop"
```

---

### Task 13: Benchmark case + agent prompt verification

**Files:**
- Create: `backend/tests/agent_benchmarks/benchmark_cases/vs_mcp/workspace_owasp_validate.yaml`

- [ ] **Step 1: Write the benchmark case YAML**

```yaml
# backend/tests/agent_benchmarks/benchmark_cases/vs_mcp/workspace_owasp_validate.yaml
# Verifies: agent applies a patch with an OWASP injection, validate fires
# automatically, agent narrates the OWASP citation from oracle/owasp partition,
# and the mechanical-fix classifier correctly REFUSES to auto-fix (judgment
# call). Note: this case requires a seeded workspace fixture with a known
# vulnerable file to be reliable.

case_id: workspace_owasp_validate
query: |
  Apply changeset {{seeded_owasp_changeset_id}} to the workspace. Walk me
  through what suitecloud project:validate finds.
tags: ["workspace", "validate", "owasp", "knowledge-profile"]
notes: |
  Tests the Validate UX end-to-end in the agent's narration. Expected agent
  behavior:
   1. Call workspace_apply_patch with the changeset.
   2. Wait for the auto-validate to complete (orchestrator-mediated).
   3. Read the resulting ValidationHit rows.
   4. Narrate the OWASP-A03 hit with a citation from oracle/owasp.
   5. Do NOT auto-propose a fix patch (OWASP is judgment-only per the
      mechanical-fix classifier).

expected_answer_contains:
  - "OWASP"
  - "owasp"  # citation reference
  - "validate"
expected_tools:
  - "workspace_apply_patch"
  - "workspace_run_validate"  # if the agent re-runs validate to confirm fix
expected_accuracy: 0.7
max_cost: 1.00
max_latency_ms: 180000
baseline_expected_accuracy: 0.0  # Claude+MCP doesn't have workspace_*  tools
baseline_expected_tools: []
```

- [ ] **Step 2: Run the new case locally to confirm it executes**

Run:
```bash
cd backend && set -a && source ../.env && set +a && .venv/bin/python -m app.services.benchmarks.run_vs_mcp \
  --case workspace_owasp_validate \
  --tenant-id ce3dfaad-626f-4992-84e9-500c8291ca0a
```
Expected: case runs end-to-end (it's OK if accuracy < 1.00 on first run; the goal is structural — case is wired up correctly).

- [ ] **Step 3: Add seeded fixture (workspace + OWASP-injected file + draft changeset)**

Add a `tests/agent_benchmarks/fixtures/seed_owasp_workspace.py` that creates:
- A `Workspace` row for the test tenant.
- A `WorkspaceFile` containing `query.runSuiteQL("SELECT * FROM transactions WHERE id = '" + req.id + "'")` — the classic OWASP-A03 sample.
- A draft `WorkspaceChangeSet` referencing the file with `status="draft"`.

The fixture is invoked at benchmark startup via the existing `--tenant-id` resolver (extend `agent_runner.py` if a fixture-loader hook isn't already there).

- [ ] **Step 4: Commit**

```bash
git add backend/tests/agent_benchmarks/benchmark_cases/vs_mcp/workspace_owasp_validate.yaml backend/tests/agent_benchmarks/fixtures/seed_owasp_workspace.py
git commit -m "test(benchmark): vs_mcp case for workspace OWASP validate narration"
```

---

## Final integration: open the PR

After Task 13:

- [ ] **Run the full backend test suite**
```bash
cd backend && .venv/bin/python -m pytest -x
```
Expected: all tests pass (~2,846 + ~30 new from this plan).

- [ ] **Run the frontend test suite**
```bash
cd frontend && npx vitest run
```
Expected: all tests pass.

- [ ] **Run the pricing soak benchmark + sales suite to verify no regressions in unrelated knowledge profiles**
```bash
cd backend && set -a && source ../.env && set +a && .venv/bin/python -m app.services.benchmarks.run_vs_mcp --suite sales --tenant-id ce3dfaad-626f-4992-84e9-500c8291ca0a --skip-baseline --no-llm-judge
```
Expected: 18/18 cases pass.

- [ ] **Push + open PR**
```bash
git push
gh pr create --title "feat(workspace): in-app validate UX with Oracle policy guidance" --body "Implements docs/superpowers/specs/2026-05-09-workspace-validate-ux-design.md. Replaces sdf validate with suitecloud project:validate --server, surfaces structured hits in runs panel + chat thread with Oracle citations from RAG, auto-fires on apply_patch and deploy attempts, auto-proposes fixes for mechanically-fixable hits."
```

---

## Spec coverage checklist (run before declaring done)

Match each spec section to a task:

- ✅ Q1 multi-layer scope → Task 4 (runner) + Task 10 (agent narration)
- ✅ Q2 surfacing in runs panel + chat → Task 11 (frontend) + Task 10 (agent)
- ✅ Q3 triggers (auto-after-apply + auto-on-deploy + retry) → Task 8 (apply_patch) + Task 9 (deploy gate) + Task 11 (retry button)
- ✅ Q4 errors block, warnings advisory → Task 4 (`gate_status`) + Task 9 (deploy_service evaluation)
- ✅ Q5 narrate + auto-propose for fixable → Task 7 (classifier) + Task 10 (agent helpers)
- ✅ Q6 all 7 oracle/* partitions → already in `suitescript_workspace.yaml`; spec verified, no task needed
- ✅ Q7 server mode only, no fallback → Task 4 (allowlist swap, `sdf_validate` removed)

Codex constraints:
- ✅ Snapshot-hash freshness → Task 4 (compute) + Task 9 (consume)
- ✅ Debounce + loop budget + fingerprint dedup → Task 6
- ✅ Deny-by-default classifier → Task 7
- ✅ Best-effort parser + raw fallback → Task 2
- ✅ First-class structured findings storage → Task 1
- ✅ `has_errors`/`has_warnings`/`gate_status` → Task 1 + Task 4
- ✅ No silent SDF fallback → Task 4 (allowlist removed entirely)
- ✅ Batched-by-family chat narration → Task 10 (`_batch_hits_by_family`)
- ✅ 180s timeout → Task 4 (allowlist entry)
- ✅ NetSuite auth wiring → Task 3
- ✅ CLI install + startup smoke check → Task 5
