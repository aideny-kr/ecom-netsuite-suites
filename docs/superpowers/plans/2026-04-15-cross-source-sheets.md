# Cross-Source Queries + Google Sheets Connector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable the unified agent to answer questions spanning NetSuite + BigQuery in one turn, and write query results to Google Sheets.

**Architecture:** Update the disambiguation instruction to encourage cross-source tool calls. Add Google Sheets as a local connector (same pattern as BigQuery): service account auth, 2 Python tool executors (`sheets_create`, `sheets_write_range`), knowledge profile. Frontend gets a Sheets connector card in Settings and a `sheets_link` SSE event card in chat.

**Tech Stack:** FastAPI, google-api-python-client, google-auth, asyncio.to_thread(), Pydantic v2, pytest, React/TypeScript

**Spec:** `docs/superpowers/specs/2026-04-15-cross-source-sheets-design.md`

---

## File Structure

| File | Responsibility |
|------|----------------|
| `backend/app/services/chat/prompt_assembler.py` (Modify) | Update disambiguation to encourage cross-source |
| `backend/app/services/chat/knowledge_profiles/cross_source.yaml` (Create) | Cross-source join-key guidance profile |
| `backend/app/services/sheets_service.py` (Create) | Google Sheets API wrapper (create, write, share) |
| `backend/app/mcp/tools/sheets_tools.py` (Create) | Tool executors for sheets_create, sheets_write_range |
| `backend/app/mcp/registry.py` (Modify) | Register sheets tools in TOOL_REGISTRY |
| `backend/app/services/chat/tools.py` (Modify) | Add google_sheets to _CONNECTOR_GATED_TOOLS |
| `backend/app/services/chat/tool_categories.py` (Modify) | Add sheets category |
| `backend/app/services/chat/knowledge_profiles/google_sheets.yaml` (Create) | Sheets knowledge profile |
| `backend/app/mcp/governance.py` (Modify) | Add tool configs for sheets tools |
| `backend/tests/test_sheets_service.py` (Create) | Service layer tests |
| `backend/tests/test_sheets_tools.py` (Create) | Tool executor tests |
| `backend/tests/test_cross_source_disambiguation.py` (Create) | Disambiguation prompt tests |
| `frontend/src/components/settings/sheets-connector-card.tsx` (Create) | Settings UI for Sheets connector |
| `frontend/src/components/chat/sheets-link-card.tsx` (Create) | Chat SSE card for Sheet URLs |

---

### Task 1: Update Disambiguation Instruction + Tests

**Files:**
- Modify: `backend/app/services/chat/prompt_assembler.py`
- Create: `backend/tests/test_cross_source_disambiguation.py`
- Create: `backend/app/services/chat/knowledge_profiles/cross_source.yaml`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_cross_source_disambiguation.py
import pytest
from app.services.chat.knowledge_profiles.loader import KnowledgeProfile
from app.services.chat.prompt_assembler import build_disambiguation_instruction


_BQ_PROFILE = KnowledgeProfile(
    profile_id="bigquery",
    display_name="BigQuery Analytics",
    trigger_tools=["bigquery_sql"],
    prompt_fragment="## BQ",
    rag_partitions=[],
)
_NS_PROFILE = KnowledgeProfile(
    profile_id="netsuite_writes",
    display_name="NetSuite",
    trigger_tools=["ext__*__ns_createRecord"],
    prompt_fragment="## NS",
    rag_partitions=[],
)


class TestCrossSourceDisambiguation:
    def test_encourages_both_sources(self):
        result = build_disambiguation_instruction([_BQ_PROFILE, _NS_PROFILE])
        assert "call both tools" in result.lower() or "use both" in result.lower()

    def test_does_not_default_to_asking_user(self):
        result = build_disambiguation_instruction([_BQ_PROFILE, _NS_PROFILE])
        assert "which would you prefer" not in result.lower()

    def test_still_suggests_asking_when_genuinely_ambiguous(self):
        result = build_disambiguation_instruction([_BQ_PROFILE, _NS_PROFILE])
        assert "ask" in result.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/python -m pytest tests/test_cross_source_disambiguation.py -v`
Expected: FAIL (current disambiguation says "which would you prefer")

- [ ] **Step 3: Update the disambiguation instruction**

In `backend/app/services/chat/prompt_assembler.py`, replace the `DISAMBIGUATION_INSTRUCTION` constant:

```python
DISAMBIGUATION_INSTRUCTION = """

## Multiple Data Sources Available
You have access to multiple data sources. Choose the best source based on the query:
- NetSuite: transactional data (orders, invoices, customers, inventory, financial reports)
- BigQuery: analytics, marketing, aggregated metrics, third-party data

If the question clearly requires data from both sources, call both tools and synthesize the results.
Identify the join key (SKU, customer email, order ID, date range) to correlate cross-source data.
If the query can be fully answered by one source, use the most authoritative one.
Only ask the user if you genuinely cannot determine which source(s) to use.
"""
```

- [ ] **Step 4: Create cross_source.yaml knowledge profile**

```yaml
# backend/app/services/chat/knowledge_profiles/cross_source.yaml
profile_id: cross_source
display_name: "Cross-Source Queries"
trigger_tools:
  - bigquery_sql
  - netsuite_suiteql
prompt_fragment: |
  ## Cross-Source Query Guidance

  When the user's question spans both NetSuite and BigQuery data:
  1. Query each source independently (call both tools)
  2. Identify the join key: SKU/item name, customer email, date range, order ID
  3. Correlate the results in your response — present a unified answer, not two separate tables
  4. If row counts differ significantly between sources, explain why (e.g., BigQuery has marketing data NetSuite doesn't track)
rag_partitions: []
```

Note: This profile activates only when BOTH bigquery_sql AND netsuite_suiteql are in the tool set (both trigger_tools must match). The `matches_tools` method returns True if ANY trigger tool matches, so this profile will activate whenever either tool is present. That's acceptable — the prompt fragment only applies guidance when both sources are queried.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && .venv/bin/python -m pytest tests/test_cross_source_disambiguation.py tests/test_prompt_assembler.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/chat/prompt_assembler.py backend/tests/test_cross_source_disambiguation.py backend/app/services/chat/knowledge_profiles/cross_source.yaml
git commit -m "feat(agent): update disambiguation to encourage cross-source queries + cross_source profile"
```

---

### Task 2: Google Sheets Service Layer + Tests

**Files:**
- Create: `backend/app/services/sheets_service.py`
- Create: `backend/tests/test_sheets_service.py`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_sheets_service.py
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from app.services.sheets_service import (
    create_spreadsheet,
    write_range,
    share_spreadsheet,
    validate_connection,
)


class TestCreateSpreadsheet:
    @pytest.mark.asyncio
    async def test_returns_spreadsheet_id_and_url(self):
        mock_service = MagicMock()
        mock_service.spreadsheets().create().execute.return_value = {
            "spreadsheetId": "abc123",
            "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/abc123",
        }
        with patch("app.services.sheets_service._build_sheets_service", return_value=mock_service):
            result = await create_spreadsheet(
                credentials={"type": "service_account"},
                title="Test Sheet",
            )
        assert result["spreadsheet_id"] == "abc123"
        assert "docs.google.com" in result["url"]

    @pytest.mark.asyncio
    async def test_raises_on_missing_credentials(self):
        with pytest.raises(ValueError, match="credentials"):
            await create_spreadsheet(credentials=None, title="Test")


class TestWriteRange:
    @pytest.mark.asyncio
    async def test_writes_data_and_returns_updated_range(self):
        mock_service = MagicMock()
        mock_service.spreadsheets().values().update().execute.return_value = {
            "updatedRange": "Sheet1!A1:C3",
            "updatedRows": 3,
            "updatedColumns": 3,
        }
        with patch("app.services.sheets_service._build_sheets_service", return_value=mock_service):
            result = await write_range(
                credentials={"type": "service_account"},
                spreadsheet_id="abc123",
                data=[["Name", "Age"], ["Alice", 30], ["Bob", 25]],
            )
        assert result["updated_rows"] == 3

    @pytest.mark.asyncio
    async def test_rejects_empty_data(self):
        with pytest.raises(ValueError, match="data"):
            await write_range(
                credentials={"type": "service_account"},
                spreadsheet_id="abc123",
                data=[],
            )


class TestShareSpreadsheet:
    @pytest.mark.asyncio
    async def test_shares_with_email(self):
        mock_drive = MagicMock()
        mock_drive.permissions().create().execute.return_value = {"id": "perm1"}
        with patch("app.services.sheets_service._build_drive_service", return_value=mock_drive):
            result = await share_spreadsheet(
                credentials={"type": "service_account"},
                spreadsheet_id="abc123",
                email="user@example.com",
            )
        assert result["permission_id"] == "perm1"


class TestValidateConnection:
    @pytest.mark.asyncio
    async def test_returns_true_on_success(self):
        mock_service = MagicMock()
        mock_service.spreadsheets().create().execute.return_value = {
            "spreadsheetId": "test123",
        }
        mock_drive = MagicMock()
        mock_drive.files().delete().execute.return_value = None
        with patch("app.services.sheets_service._build_sheets_service", return_value=mock_service), \
             patch("app.services.sheets_service._build_drive_service", return_value=mock_drive):
            result = await validate_connection(credentials={"type": "service_account"})
        assert result["valid"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/python -m pytest tests/test_sheets_service.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement the service**

```python
# backend/app/services/sheets_service.py
"""Google Sheets API wrapper using service account auth.

Synchronous google-api-python-client calls wrapped with asyncio.to_thread()
to avoid blocking the event loop. Same pattern as bigquery_service.py.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]


def _build_sheets_service(credentials: dict):
    creds = service_account.Credentials.from_service_account_info(credentials, scopes=_SCOPES)
    return build("sheets", "v4", credentials=creds)


def _build_drive_service(credentials: dict):
    creds = service_account.Credentials.from_service_account_info(credentials, scopes=_SCOPES)
    return build("drive", "v3", credentials=creds)


async def create_spreadsheet(*, credentials: dict | None, title: str) -> dict[str, str]:
    if not credentials:
        raise ValueError("credentials required")

    def _sync():
        service = _build_sheets_service(credentials)
        result = service.spreadsheets().create(
            body={"properties": {"title": title}},
            fields="spreadsheetId,spreadsheetUrl",
        ).execute()
        return {
            "spreadsheet_id": result["spreadsheetId"],
            "url": result["spreadsheetUrl"],
        }

    return await asyncio.to_thread(_sync)


async def write_range(
    *,
    credentials: dict | None,
    spreadsheet_id: str,
    data: list[list[Any]],
    range_str: str = "Sheet1!A1",
) -> dict[str, Any]:
    if not credentials:
        raise ValueError("credentials required")
    if not data:
        raise ValueError("data must be non-empty")

    def _sync():
        service = _build_sheets_service(credentials)
        result = service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=range_str,
            valueInputOption="RAW",
            body={"values": data},
        ).execute()
        return {
            "updated_range": result.get("updatedRange", ""),
            "updated_rows": result.get("updatedRows", 0),
            "updated_columns": result.get("updatedColumns", 0),
        }

    return await asyncio.to_thread(_sync)


async def share_spreadsheet(
    *,
    credentials: dict | None,
    spreadsheet_id: str,
    email: str,
    role: str = "writer",
) -> dict[str, str]:
    if not credentials:
        raise ValueError("credentials required")

    def _sync():
        drive = _build_drive_service(credentials)
        result = drive.permissions().create(
            fileId=spreadsheet_id,
            body={"type": "user", "role": role, "emailAddress": email},
            sendNotificationEmail=False,
        ).execute()
        return {"permission_id": result["id"]}

    return await asyncio.to_thread(_sync)


async def validate_connection(*, credentials: dict | None) -> dict[str, Any]:
    if not credentials:
        raise ValueError("credentials required")

    def _sync():
        sheets = _build_sheets_service(credentials)
        result = sheets.spreadsheets().create(
            body={"properties": {"title": "AI-den Connection Test"}},
            fields="spreadsheetId",
        ).execute()
        test_id = result["spreadsheetId"]
        drive = _build_drive_service(credentials)
        drive.files().delete(fileId=test_id).execute()
        return {"valid": True}

    try:
        return await asyncio.to_thread(_sync)
    except Exception as e:
        logger.warning("sheets_service.validate_connection_failed", exc_info=True)
        return {"valid": False, "error": str(e)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/bin/python -m pytest tests/test_sheets_service.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sheets_service.py backend/tests/test_sheets_service.py
git commit -m "feat(sheets): add Google Sheets service layer — create, write, share, validate"
```

---

### Task 3: Sheets Tool Executors + Registry

**Files:**
- Create: `backend/app/mcp/tools/sheets_tools.py`
- Create: `backend/tests/test_sheets_tools.py`
- Modify: `backend/app/mcp/registry.py`
- Modify: `backend/app/mcp/governance.py`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_sheets_tools.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.mcp.tools.sheets_tools import sheets_create_execute, sheets_write_range_execute


_CONTEXT = {
    "tenant_id": "ce3dfaad-626f-4992-84e9-500c8291ca0a",
    "actor_id": "1e864ab2-2310-47f8-b50d-1424e407ae03",
    "db": AsyncMock(),
    "correlation_id": "test",
}


class TestSheetsCreateExecute:
    @pytest.mark.asyncio
    async def test_returns_spreadsheet_url(self):
        with patch("app.mcp.tools.sheets_tools._get_sheets_connector") as mock_conn, \
             patch("app.mcp.tools.sheets_tools.create_spreadsheet") as mock_create, \
             patch("app.mcp.tools.sheets_tools.share_spreadsheet") as mock_share:
            mock_conn.return_value = MagicMock(
                encrypted_credentials="encrypted",
            )
            mock_create.return_value = {
                "spreadsheet_id": "abc123",
                "url": "https://docs.google.com/spreadsheets/d/abc123",
            }
            mock_share.return_value = {"permission_id": "perm1"}
            result = await sheets_create_execute(
                {"title": "Test Sheet"},
                _CONTEXT,
            )
        assert result["spreadsheet_id"] == "abc123"
        assert "url" in result

    @pytest.mark.asyncio
    async def test_returns_error_when_no_connector(self):
        with patch("app.mcp.tools.sheets_tools._get_sheets_connector", return_value=None):
            result = await sheets_create_execute({"title": "Test"}, _CONTEXT)
        assert result["error"] is True


class TestSheetsWriteRangeExecute:
    @pytest.mark.asyncio
    async def test_writes_data(self):
        with patch("app.mcp.tools.sheets_tools._get_sheets_connector") as mock_conn, \
             patch("app.mcp.tools.sheets_tools.write_range") as mock_write:
            mock_conn.return_value = MagicMock(encrypted_credentials="encrypted")
            mock_write.return_value = {"updated_rows": 3, "updated_range": "Sheet1!A1:B3", "updated_columns": 2}
            result = await sheets_write_range_execute(
                {
                    "spreadsheet_id": "abc123",
                    "data": [["Name", "Value"], ["A", 1], ["B", 2]],
                },
                _CONTEXT,
            )
        assert result["updated_rows"] == 3

    @pytest.mark.asyncio
    async def test_returns_error_on_empty_data(self):
        with patch("app.mcp.tools.sheets_tools._get_sheets_connector") as mock_conn:
            mock_conn.return_value = MagicMock(encrypted_credentials="encrypted")
            result = await sheets_write_range_execute(
                {"spreadsheet_id": "abc123", "data": []},
                _CONTEXT,
            )
        assert result["error"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && .venv/bin/python -m pytest tests/test_sheets_tools.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement tool executors**

```python
# backend/app/mcp/tools/sheets_tools.py
"""Google Sheets tool executors for the chat agent.

Same pattern as bigquery_tools.py: async functions taking (params, context),
looking up the active connector, decrypting credentials, calling the service.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import decrypt_credentials
from app.models.mcp_connector import McpConnector
from app.services.sheets_service import (
    create_spreadsheet,
    share_spreadsheet,
    write_range,
)

logger = logging.getLogger(__name__)


async def _get_sheets_connector(context: dict) -> McpConnector | None:
    db: AsyncSession = context["db"]
    tenant_id = uuid.UUID(context["tenant_id"]) if isinstance(context["tenant_id"], str) else context["tenant_id"]
    result = await db.execute(
        select(McpConnector).where(
            McpConnector.tenant_id == tenant_id,
            McpConnector.provider == "google_sheets",
            McpConnector.status == "active",
            McpConnector.is_enabled.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def _get_user_email(context: dict) -> str | None:
    db: AsyncSession = context["db"]
    actor_id = uuid.UUID(context["actor_id"]) if isinstance(context["actor_id"], str) else context["actor_id"]
    from app.models.user import User

    result = await db.execute(select(User.email).where(User.id == actor_id))
    return result.scalar_one_or_none()


async def sheets_create_execute(params: dict, context: dict, **kwargs: Any) -> dict:
    connector = await _get_sheets_connector(context)
    if not connector:
        return {"error": True, "message": "Google Sheets connector not configured. Set up in Settings."}

    credentials = decrypt_credentials(connector.encrypted_credentials)
    title = params.get("title", "AI-den Export")

    try:
        result = await create_spreadsheet(credentials=credentials, title=title)
    except Exception as e:
        logger.warning("sheets_tools.create_failed", exc_info=True)
        return {"error": True, "message": f"Failed to create spreadsheet: {e}"}

    user_email = await _get_user_email(context)
    if user_email:
        try:
            await share_spreadsheet(
                credentials=credentials,
                spreadsheet_id=result["spreadsheet_id"],
                email=user_email,
            )
        except Exception:
            logger.warning("sheets_tools.share_failed", exc_info=True)

    return {
        "error": False,
        "spreadsheet_id": result["spreadsheet_id"],
        "url": result["url"],
        "shared_with": user_email,
    }


async def sheets_write_range_execute(params: dict, context: dict, **kwargs: Any) -> dict:
    connector = await _get_sheets_connector(context)
    if not connector:
        return {"error": True, "message": "Google Sheets connector not configured."}

    spreadsheet_id = params.get("spreadsheet_id")
    data = params.get("data", [])
    range_str = params.get("range", "Sheet1!A1")

    if not spreadsheet_id:
        return {"error": True, "message": "spreadsheet_id is required."}
    if not data:
        return {"error": True, "message": "data must be a non-empty 2D array."}

    credentials = decrypt_credentials(connector.encrypted_credentials)

    try:
        result = await write_range(
            credentials=credentials,
            spreadsheet_id=spreadsheet_id,
            data=data,
            range_str=range_str,
        )
    except Exception as e:
        logger.warning("sheets_tools.write_failed", exc_info=True)
        return {"error": True, "message": f"Failed to write to spreadsheet: {e}"}

    return {
        "error": False,
        "updated_rows": result["updated_rows"],
        "updated_columns": result["updated_columns"],
        "updated_range": result["updated_range"],
    }
```

- [ ] **Step 4: Register tools in registry**

Add to `backend/app/mcp/registry.py` in the `TOOL_REGISTRY` dict:

```python
from app.mcp.tools import sheets_tools

# In TOOL_REGISTRY:
"sheets.create": {
    "description": "Create a new Google Spreadsheet. Returns the spreadsheet ID and URL. The sheet is automatically shared with the requesting user.",
    "execute": sheets_tools.sheets_create_execute,
    "params_schema": {
        "title": {"type": "string", "required": True, "description": "Title for the new spreadsheet"},
    },
},
"sheets.write_range": {
    "description": "Write data to a Google Spreadsheet. Data should be a 2D array where row 0 is headers. Returns the updated range and row count.",
    "execute": sheets_tools.sheets_write_range_execute,
    "params_schema": {
        "spreadsheet_id": {"type": "string", "required": True, "description": "ID of the spreadsheet to write to"},
        "data": {"type": "array", "required": True, "description": "2D array of values. Row 0 should be column headers."},
        "range": {"type": "string", "required": False, "description": "Cell range to write to (default: Sheet1!A1)"},
    },
},
```

Also add these tools to `ALLOWED_CHAT_TOOLS` in `backend/app/services/chat/nodes.py`:

```python
ALLOWED_CHAT_TOOLS.update({"sheets.create", "sheets.write_range"})
```

- [ ] **Step 5: Add connector gating in tools.py**

In `backend/app/services/chat/tools.py`, add to `_CONNECTOR_GATED_TOOLS`:

```python
_CONNECTOR_GATED_TOOLS: dict[str, set[str]] = {
    "bigquery": {"bigquery_sql", "bigquery_schema", "bigquery_cost_estimate"},
    "google_sheets": {"sheets_create", "sheets_write_range"},
}
```

- [ ] **Step 6: Add tool category in tool_categories.py**

In `backend/app/services/chat/tool_categories.py`, add to `_EXACT`:

```python
"sheets_create": "sheets",
"sheets.create": "sheets",
"sheets_write_range": "sheets",
"sheets.write_range": "sheets",
```

- [ ] **Step 7: Add governance config in governance.py**

Add tool configs for sheets tools:

```python
"sheets.create": {
    "timeout_seconds": 15,
    "rate_limit_per_minute": 20,
    "requires_entitlement": "mcp_tools",
    "allowlisted_params": ["title"],
},
"sheets.write_range": {
    "timeout_seconds": 30,
    "rate_limit_per_minute": 20,
    "requires_entitlement": "mcp_tools",
    "allowlisted_params": ["spreadsheet_id", "data", "range"],
},
```

- [ ] **Step 8: Run tests**

Run: `cd backend && .venv/bin/python -m pytest tests/test_sheets_tools.py tests/test_sheets_service.py -v`
Expected: All PASS

- [ ] **Step 9: Commit**

```bash
git add backend/app/mcp/tools/sheets_tools.py backend/app/mcp/registry.py backend/app/services/chat/tools.py backend/app/services/chat/tool_categories.py backend/app/mcp/governance.py backend/tests/test_sheets_tools.py
git commit -m "feat(sheets): add sheets_create and sheets_write_range tool executors + registry"
```

---

### Task 4: Google Sheets Knowledge Profile

**Files:**
- Create: `backend/app/services/chat/knowledge_profiles/google_sheets.yaml`

- [ ] **Step 1: Create the profile**

```yaml
# backend/app/services/chat/knowledge_profiles/google_sheets.yaml
profile_id: google_sheets
display_name: "Google Sheets"
trigger_tools:
  - sheets_create
  - sheets_write_range
prompt_fragment: |
  ## Google Sheets Context

  You can create Google Sheets and write data to them. When the user asks to export data to a Sheet:
  1. First query the data (SuiteQL, BigQuery, etc.)
  2. Call sheets_create with a descriptive title
  3. Call sheets_write_range with headers in row 0 followed by data rows
  4. The Sheet URL is returned by sheets_create — present it to the user

  Data format: pass a 2D array where element 0 is headers. Numbers as numbers, not strings.
  If the query returned >1000 rows, warn the user before writing.
  If sheets_write_range fails, tell the user and offer to retry or download as CSV instead.
rag_partitions: []
```

- [ ] **Step 2: Verify profile loads**

Run: `cd backend && .venv/bin/python -c "from app.services.chat.knowledge_profiles import load_all_profiles; ps = load_all_profiles(); print(f'{len(ps)} profiles: {[p.profile_id for p in ps]}')"`
Expected: `6 profiles: ['bigquery', 'cross_source', 'google_sheets', 'netsuite_writes', 'pricing', 'reconciliation']`

- [ ] **Step 3: Commit**

```bash
git add backend/app/services/chat/knowledge_profiles/google_sheets.yaml
git commit -m "feat(sheets): add google_sheets knowledge profile"
```

---

### Task 5: Sheets SSE Event + Orchestrator Interception

**Files:**
- Modify: `backend/app/services/chat/orchestrator.py`
- Modify: `frontend/src/components/chat/sheets-link-card.tsx` (Create)
- Modify: `frontend/src/components/chat/message-list.tsx`
- Modify: `frontend/src/lib/chat-stream.ts`

- [ ] **Step 1: Add sheets_link interception in orchestrator**

In `backend/app/services/chat/orchestrator.py`, in the `_intercept_tool_result` function, add a case for sheets tools. The sheets_create tool returns a URL that should be sent to the frontend as a `sheets_link` SSE event:

```python
# After the data_table interception block:
if tool_name in ("sheets_create", "sheets.create"):
    try:
        parsed = json.loads(result_str)
        if not parsed.get("error") and parsed.get("url"):
            return "sheets_link", {
                "url": parsed["url"],
                "spreadsheet_id": parsed.get("spreadsheet_id", ""),
                "title": parsed.get("title", "Spreadsheet"),
            }, result_str  # Pass full result to LLM so it can reference the URL
    except (json.JSONDecodeError, KeyError):
        pass
```

- [ ] **Step 2: Add sheets_link to frontend normalizeStreamMessage**

In `frontend/src/lib/chat-stream.ts`, ensure `sheets_link` structured output is preserved in `normalizeStreamMessage` (same pattern as `data_table` and `write_confirmation`).

- [ ] **Step 3: Create SheetsLinkCard component**

```tsx
// frontend/src/components/chat/sheets-link-card.tsx
"use client";

import { ExternalLink, FileSpreadsheet } from "lucide-react";

interface SheetsLinkCardProps {
  url: string;
  title?: string;
}

export function SheetsLinkCard({ url, title }: SheetsLinkCardProps) {
  return (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      className="flex items-center gap-3 rounded-xl border bg-card p-4 shadow-soft hover:bg-accent/50 transition-colors"
    >
      <FileSpreadsheet className="h-5 w-5 text-green-600 shrink-0" />
      <div className="flex-1 min-w-0">
        <p className="text-[15px] font-medium text-foreground truncate">
          {title || "Google Sheet"}
        </p>
        <p className="text-[13px] text-muted-foreground truncate">{url}</p>
      </div>
      <ExternalLink className="h-4 w-4 text-muted-foreground shrink-0" />
    </a>
  );
}
```

- [ ] **Step 4: Wire SheetsLinkCard into message-list.tsx**

Add rendering for `sheets_link` structured output type in the assistant message component, similar to how `write_confirmation` is rendered.

- [ ] **Step 5: Run frontend tests**

Run: `cd frontend && npx vitest run`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/chat/orchestrator.py frontend/src/components/chat/sheets-link-card.tsx frontend/src/components/chat/message-list.tsx frontend/src/lib/chat-stream.ts
git commit -m "feat(sheets): add sheets_link SSE event + frontend card"
```

---

### Task 6: Frontend Sheets Connector Card in Settings

**Files:**
- Create: `frontend/src/components/settings/sheets-connector-card.tsx`
- Modify: `frontend/src/components/settings/data-source-connectors-section.tsx`

- [ ] **Step 1: Create SheetsConnectorCard**

Copy the BigQuery connector card pattern (`bigquery-connection-section.tsx`). Simplify for Sheets:
- Upload service account JSON file
- Test connection button (calls `validate_connection`)
- Save → creates `mcp_connectors` entry with `provider: "google_sheets"`
- Show connected/disconnected status badge

No table selector needed (unlike BigQuery). The card is simpler.

- [ ] **Step 2: Wire into data-source-connectors-section.tsx**

Add the SheetsConnectorCard alongside the BigQuery and Stripe connector cards.

- [ ] **Step 3: Run frontend tests**

Run: `cd frontend && npx vitest run`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/settings/sheets-connector-card.tsx frontend/src/components/settings/data-source-connectors-section.tsx
git commit -m "feat(sheets): add Google Sheets connector card in Settings"
```

---

### Task 7: Add google-api-python-client Dependency

**Files:**
- Modify: `backend/pyproject.toml`

- [ ] **Step 1: Add dependency**

```bash
cd backend && .venv/bin/pip install google-api-python-client google-auth-httplib2
```

Add to `pyproject.toml` dependencies:
```toml
"google-api-python-client>=2.100.0",
"google-auth-httplib2>=0.2.0",
```

- [ ] **Step 2: Verify import works**

Run: `cd backend && .venv/bin/python -c "from googleapiclient.discovery import build; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add backend/pyproject.toml
git commit -m "chore: add google-api-python-client dependency for Sheets integration"
```

Note: This task should be done FIRST if running tasks sequentially, since Tasks 2-3 import from the package. Listed here for logical grouping but implement early.

---

### Task 8: Full Backend Test Suite + Integration Verification

- [ ] **Step 1: Run full backend test suite**

Run: `cd backend && .venv/bin/python -m pytest -x -q`
Expected: All PASS

- [ ] **Step 2: Run prompt tool sync invariant**

Run: `cd backend && .venv/bin/python -m pytest tests/test_prompt_tool_sync.py -v`
Expected: All PASS

- [ ] **Step 3: Verify knowledge profiles load**

Run: `cd backend && .venv/bin/python -c "from app.services.chat.knowledge_profiles import load_all_profiles; ps = load_all_profiles(); print(f'{len(ps)} profiles: {[p.profile_id for p in ps]}')"`
Expected: 6 profiles including cross_source and google_sheets

- [ ] **Step 4: Run frontend tests**

Run: `cd frontend && npx vitest run`
Expected: All PASS

- [ ] **Step 5: Commit (if any fixes needed)**

```bash
git commit -m "test: verify full suite passes with cross-source + sheets changes"
```
