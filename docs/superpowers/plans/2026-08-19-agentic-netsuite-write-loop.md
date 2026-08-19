# Agentic NetSuite Write Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the agent compose NetSuite write payloads that actually validate, repair them in-loop when they don't, and show the human the real payload before anything is written.

**Architecture:** A validator sits at the mutation intercept (`base_agent.py:1237`). The model composes freely; the validator checks the normalized payload against cached record-type metadata plus two posting invariants. Invalid payloads return a *structured error to the model* rather than a confirmation card, and the model repairs — bounded at 2 attempts with stall detection. On exhaustion the card renders the unresolved required fields as server-declared editable slots the human fills in.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0, Pydantic v2, pytest. Frontend: Next.js 14 App Router, React, TypeScript, Tailwind, vitest.

**Spec:** `docs/superpowers/specs/2026-08-19-agentic-netsuite-write-loop-design.md`
**ClickUp:** [86bbgnwaj](https://app.clickup.com/t/86bbgnwaj). Subsumes [86bbgnw8h](https://app.clickup.com/t/86bbgnw8h) (Task 2) and [86bbgnw9g](https://app.clickup.com/t/86bbgnw9g) (Task 8).

## Global Constraints

- **TDD is mandatory.** Write the failing test first, run it, prove it fails for the right reason, then implement. A test never shown red is not a test.
- **Tier T2** — mutates customer data, HITL invariant, financial posting. Blocking multi-angle review pre-merge: `Workflow({name:"code-review-multiangle", args:{target:"<PR#>"}})`.
- **Backend tests:** `backend/.venv/bin/python -m pytest`. DB-backed tests need `dangerouslyDisableSandbox`.
- **Frontend tests:** `cd frontend && npx vitest run`.
- **Never** hardcode NetSuite field names in Python. Every required-field fact comes from `ns_getRecordTypeMetadata` at runtime. The only hardcoded domain constants permitted are the two invariants in Task 5.
- **Fail closed on rendering:** an unparseable payload must block the write. It must never produce an empty-but-approvable card.
- **Fail open on field validation:** if metadata is unavailable, skip *field* validation and mark the card `unvalidated`. The two posting invariants still run regardless.
- Commit after every task. One commit per logical change. Never amend.
- Do not modify `_BLOCKED_RECORD_TYPES`.

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/app/services/chat/write_payload.py` | **Create.** Normalize any MCP tool_input shape into `NormalizedPayload`. Raise `PayloadParseError` when it cannot. |
| `backend/app/services/chat/record_metadata_service.py` | **Create.** Fetch + TTL-cache `ns_getRecordTypeMetadata`; resolve option sets for editable slots. |
| `backend/app/services/chat/write_validator.py` | **Create.** Validate a `NormalizedPayload` against `RecordMetadata` + posting invariants. Produce `ValidationResult`. |
| `backend/app/services/chat/write_confirmation_service.py` | **Modify.** Use the normalizer; carry `editable_slots` + `unvalidated`; add `merge_slot_values`. |
| `backend/app/services/chat/agents/base_agent.py:1234-1291` | **Modify.** Insert the bounded repair loop into the mutation intercept. |
| `backend/app/services/chat/orchestrator.py:1741-1811` | **Modify.** Accept slot values on approve; terminal `failed` status. |
| `backend/app/services/chat/knowledge_profiles/netsuite_writes.yaml` | **Modify.** Teach metadata-first composition (guidance complementing the validator, not replacing it). |
| `frontend/src/components/chat/write-confirmation-card.tsx` | **Modify.** Render real fields; render editable slots as a form; render `failed` + `unvalidated` states. |
| `frontend/src/lib/types.ts` | **Modify.** Extend `WriteConfirmationData`. |

---

### Task 1: Payload normalizer

**Files:**
- Create: `backend/app/services/chat/write_payload.py`
- Test: `backend/tests/test_write_payload.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `NormalizedPayload(fields: dict, lines: list[dict], record_id: str | None)`, `normalize_write_payload(tool_input: dict) -> NormalizedPayload`, `PayloadParseError`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_write_payload.py
import pytest
from app.services.chat.write_payload import (
    NormalizedPayload,
    PayloadParseError,
    normalize_write_payload,
)


def test_data_as_json_string_is_parsed():
    """The live NetSuite MCP shape — `data` holding a JSON *string*."""
    result = normalize_write_payload(
        {"recordType": "customer", "data": '{"companyname": "test ai customer"}'}
    )
    assert result.fields == {"companyname": "test ai customer"}
    assert result.lines == []
    assert result.record_id is None


def test_body_as_dict_is_parsed():
    """The legacy shape the old code assumed."""
    result = normalize_write_payload({"recordType": "invoice", "body": {"memo": "x"}})
    assert result.fields == {"memo": "x"}


def test_data_as_dict_is_parsed():
    result = normalize_write_payload({"recordType": "invoice", "data": {"memo": "x"}})
    assert result.fields == {"memo": "x"}


def test_record_id_from_top_level_then_body():
    assert normalize_write_payload({"id": "42", "data": "{}"}).record_id == "42"
    assert normalize_write_payload({"data": '{"id": 7}'}).record_id == "7"


def test_lines_are_extracted_from_sublists():
    """Transaction records carry lines; validation must see them."""
    result = normalize_write_payload(
        {
            "recordType": "journalEntry",
            "data": '{"subsidiary": "1", "line": [{"account": "10", "debit": 5},'
            ' {"account": "20", "credit": 5}]}',
        }
    )
    assert result.fields["subsidiary"] == "1"
    assert len(result.lines) == 2
    assert result.lines[0]["account"] == "10"


def test_unparseable_payload_raises():
    """Fail closed — never silently yield empty fields."""
    with pytest.raises(PayloadParseError):
        normalize_write_payload({"recordType": "customer", "data": "{not json"})


def test_no_payload_key_at_all_raises():
    with pytest.raises(PayloadParseError):
        normalize_write_payload({"recordType": "customer"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_write_payload.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.chat.write_payload'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/chat/write_payload.py
"""Canonical parsing of MCP write tool_input into header fields + lines.

Every NetSuite write payload enters the system through exactly one function so
a new MCP tool schema cannot silently produce an empty confirmation card. The
Oracle NetSuite MCP sends ``data`` as a JSON *string*; older/other schemas send
``body`` as a dict. Both — and dict-valued ``data`` — normalize here.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, model_validator

# Keys that hold the record payload, in precedence order.
_PAYLOAD_KEYS = ("data", "body")

# Sublist keys that carry transaction lines. NetSuite uses several names
# depending on record type; all are treated as line collections.
_LINE_KEYS = ("line", "lines", "item", "items", "expense", "apply")


class PayloadParseError(ValueError):
    """The tool input carried no parseable record payload."""


class NormalizedPayload(BaseModel):
    fields: dict[str, Any]
    lines: list[dict[str, Any]]
    record_id: str | None = None


def _coerce(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PayloadParseError(f"payload is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise PayloadParseError("payload JSON is not an object")
        return parsed
    return None


def normalize_write_payload(tool_input: dict[str, Any]) -> NormalizedPayload:
    """Return the canonical payload, or raise :class:`PayloadParseError`."""
    record: dict[str, Any] | None = None
    for key in _PAYLOAD_KEYS:
        if key in tool_input:
            record = _coerce(tool_input[key])
            if record is not None:
                break

    if record is None:
        raise PayloadParseError("tool_input carried no 'data' or 'body' payload")

    lines: list[dict[str, Any]] = []
    fields: dict[str, Any] = {}
    for key, value in record.items():
        if key in _LINE_KEYS and isinstance(value, list):
            lines.extend(item for item in value if isinstance(item, dict))
        else:
            fields[key] = value

    record_id = tool_input.get("id") or record.get("id")
    return NormalizedPayload(
        fields=fields,
        lines=lines,
        record_id=str(record_id) if record_id is not None else None,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_write_payload.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/chat/write_payload.py backend/tests/test_write_payload.py
git commit -m "feat(chat): canonical write-payload normalizer with fail-closed parsing"
```

---

### Task 2: Wire the normalizer into the confirmation card (fixes 86bbgnw8h)

**Files:**
- Modify: `backend/app/services/chat/write_confirmation_service.py:104-127`
- Test: `backend/tests/test_write_confirmation.py`

**Interfaces:**
- Consumes: `normalize_write_payload`, `PayloadParseError`, `NormalizedPayload` from Task 1.
- Produces: `build_confirmation_payload` now populates `proposed_fields` for the real MCP shape and returns `None` on unparseable input. `WriteConfirmationPayload` gains `proposed_lines: list[dict] = []`.

- [ ] **Step 1: Write the failing test**

```python
# append to backend/tests/test_write_confirmation.py
from app.services.chat.write_confirmation_service import build_confirmation_payload


def test_proposed_fields_populated_for_live_mcp_shape():
    """Regression: the card rendered empty for every NetSuite write.

    `tool_input` uses `data` (a JSON string); the old code read `body`.
    """
    payload = build_confirmation_payload(
        mutation_type="create",
        record_type="customer",
        tool_name="ext__" + "a" * 32 + "__ns_createRecord",
        tool_input={
            "recordType": "customer",
            "data": '{"companyname": "test ai customer"}',
        },
        session_id="11111111-1111-1111-1111-111111111111",
    )
    assert payload is not None
    assert payload.proposed_fields == {"companyname": "test ai customer"}


def test_lines_surface_on_the_card():
    payload = build_confirmation_payload(
        mutation_type="create",
        record_type="journalEntry",
        tool_name="ext__" + "a" * 32 + "__ns_createRecord",
        tool_input={
            "recordType": "journalEntry",
            "data": '{"subsidiary": "1", "line": [{"account": "10", "debit": 5}]}',
        },
        session_id="11111111-1111-1111-1111-111111111111",
    )
    assert payload is not None
    assert payload.proposed_lines == [{"account": "10", "debit": 5}]


def test_unparseable_payload_blocks_the_write():
    """Fail closed — no empty-but-approvable card."""
    payload = build_confirmation_payload(
        mutation_type="create",
        record_type="customer",
        tool_name="ext__" + "a" * 32 + "__ns_createRecord",
        tool_input={"recordType": "customer", "data": "{not json"},
        session_id="11111111-1111-1111-1111-111111111111",
    )
    assert payload is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_write_confirmation.py -k "live_mcp_shape or lines_surface or unparseable" -v`
Expected: FAIL — `proposed_fields == {}` (not the expected dict), and `AttributeError: proposed_lines`

- [ ] **Step 3: Write minimal implementation**

Add to `WriteConfirmationPayload` (after `proposed_fields`, line 42):

```python
    proposed_lines: list[dict[str, Any]] = []
```

Replace lines 104-127 of `build_confirmation_payload`:

```python
    if not is_record_type_allowed(record_type):
        return None

    try:
        normalized = normalize_write_payload(tool_input)
    except PayloadParseError:
        # Fail closed: an unrenderable payload must not become an
        # approvable card. Returning None makes the caller surface an error.
        return None

    payload_json = _build_payload_json(tool_name, tool_input)
    confirmation_token = generate_confirmation_token(session_id, payload_json)

    return WriteConfirmationPayload(
        mutation_type=mutation_type,
        record_type=record_type,
        record_id=normalized.record_id,
        proposed_fields=normalized.fields,
        proposed_lines=normalized.lines,
        current_record=current_record,
        tool_name=tool_name,
        tool_input=tool_input,
        confirmation_token=confirmation_token,
    )
```

Add the import at the top of the file:

```python
from app.services.chat.write_payload import PayloadParseError, normalize_write_payload
```

- [ ] **Step 4: Run the full write-confirmation suite**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_write_confirmation.py backend/tests/test_mutation_intercept.py backend/tests/test_write_confirm_orchestrator.py -v`
Expected: PASS. `build_recon_group_confirmation` is untouched and must stay green — it builds `proposed_fields` directly and does not go through the normalizer.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/chat/write_confirmation_service.py backend/tests/test_write_confirmation.py
git commit -m "fix(chat): confirmation card renders real fields for NetSuite MCP writes"
```

---

### Task 3: Record metadata service

**Files:**
- Create: `backend/app/services/chat/record_metadata_service.py`
- Test: `backend/tests/test_record_metadata_service.py`

**Interfaces:**
- Consumes: `execute_tool_call` from `app.services.chat.tools`; `_make_ext_tool_name`, `parse_external_tool_name`.
- Produces: `FieldSpec(name, label, required, type, options)`, `RecordMetadata(record_type, fields, line_fields)`, `async get_record_metadata(record_type, mutation_tool_name, tenant_id, actor_id, correlation_id, db, session_id) -> RecordMetadata | None`, `clear_metadata_cache()`.

Returns `None` when metadata cannot be fetched — callers treat that as "unvalidated", never as "no requirements".

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_record_metadata_service.py
import json
import pytest

from app.services.chat import record_metadata_service as svc

EXT = "ext__" + "a" * 32 + "__ns_createRecord"

_META = {
    "fields": [
        {"name": "companyname", "label": "Company Name", "mandatory": False, "type": "text"},
        {"name": "subsidiary", "label": "Primary Subsidiary", "mandatory": True, "type": "select"},
    ],
    "sublists": [
        {"name": "line", "fields": [{"name": "account", "label": "Account", "mandatory": True, "type": "select"}]}
    ],
}


@pytest.fixture(autouse=True)
def _clear():
    svc.clear_metadata_cache()
    yield
    svc.clear_metadata_cache()


@pytest.mark.asyncio
async def test_parses_required_fields_and_line_fields(monkeypatch):
    async def fake_exec(**kwargs):
        assert kwargs["tool_name"].endswith("ns_getRecordTypeMetadata")
        return json.dumps(_META)

    monkeypatch.setattr(svc, "execute_tool_call", fake_exec)
    meta = await svc.get_record_metadata(
        record_type="customer", mutation_tool_name=EXT, tenant_id=None,
        actor_id=None, correlation_id="c", db=None, session_id="s",
    )
    assert meta is not None
    assert [f.name for f in meta.fields if f.required] == ["subsidiary"]
    assert [f.name for f in meta.line_fields if f.required] == ["account"]


@pytest.mark.asyncio
async def test_result_is_cached(monkeypatch):
    calls = {"n": 0}

    async def fake_exec(**kwargs):
        calls["n"] += 1
        return json.dumps(_META)

    monkeypatch.setattr(svc, "execute_tool_call", fake_exec)
    kw = dict(record_type="customer", mutation_tool_name=EXT, tenant_id=None,
              actor_id=None, correlation_id="c", db=None, session_id="s")
    await svc.get_record_metadata(**kw)
    await svc.get_record_metadata(**kw)
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_fetch_failure_returns_none_not_empty(monkeypatch):
    """A failed lookup must be distinguishable from 'no required fields'."""
    async def boom(**kwargs):
        raise RuntimeError("MCP down")

    monkeypatch.setattr(svc, "execute_tool_call", boom)
    meta = await svc.get_record_metadata(
        record_type="customer", mutation_tool_name=EXT, tenant_id=None,
        actor_id=None, correlation_id="c", db=None, session_id="s",
    )
    assert meta is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_record_metadata_service.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/chat/record_metadata_service.py
"""Fetch and cache NetSuite record-type metadata for write validation.

Returning ``None`` means "could not determine requirements" and is NOT the same
as "no required fields" — callers must render the card as ``unvalidated``
rather than assuming the payload is complete.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from pydantic import BaseModel, model_validator

from app.services.chat.tools import execute_tool_call

logger = logging.getLogger(__name__)

_TTL_SECONDS = 3600
_cache: dict[tuple[str, str], tuple[float, "RecordMetadata"]] = {}


class FieldSpec(BaseModel):
    name: str
    label: str
    required: bool = False
    type: str = "text"
    options: list[dict[str, Any]] | None = None


class RecordMetadata(BaseModel):
    record_type: str
    fields: list[FieldSpec] = []
    line_fields: list[FieldSpec] = []

    def required_field_names(self) -> list[str]:
        return [f.name for f in self.fields if f.required]

    def required_line_field_names(self) -> list[str]:
        return [f.name for f in self.line_fields if f.required]

    def spec_for(self, name: str) -> FieldSpec | None:
        return next((f for f in self.fields if f.name == name), None)


def clear_metadata_cache() -> None:
    _cache.clear()


def _parse_field(raw: dict[str, Any]) -> FieldSpec:
    return FieldSpec(
        name=raw.get("name", ""),
        label=raw.get("label") or raw.get("name", ""),
        # NetSuite metadata uses "mandatory"; accept "required" defensively.
        required=bool(raw.get("mandatory") or raw.get("required")),
        type=raw.get("type", "text"),
        options=raw.get("options"),
    )


async def get_record_metadata(
    *,
    record_type: str,
    mutation_tool_name: str,
    tenant_id: Any,
    actor_id: Any,
    correlation_id: str,
    db: Any,
    session_id: str,
) -> RecordMetadata | None:
    """Return metadata for *record_type*, or ``None`` if it cannot be fetched."""
    from app.services.chat.tools import _make_ext_tool_name, parse_external_tool_name

    parsed = parse_external_tool_name(mutation_tool_name)
    if not parsed:
        return None
    connector_id = parsed[0]

    key = (str(connector_id), record_type)
    hit = _cache.get(key)
    if hit and (time.monotonic() - hit[0]) < _TTL_SECONDS:
        return hit[1]

    tool = _make_ext_tool_name(connector_id, "ns_getRecordTypeMetadata")

    # The ENTIRE fetch-and-parse body is guarded. A malformed-but-valid-JSON
    # response must degrade to None ("requirements unknown"), never raise and
    # never yield an empty RecordMetadata — an empty result would tell the
    # validator "nothing is required" and wave an invalid write straight
    # through, which is the exact bug this service exists to prevent.
    try:
        raw = await execute_tool_call(
            tool_name=tool,
            tool_input={"recordType": record_type},
            tenant_id=tenant_id,
            actor_id=actor_id,
            correlation_id=correlation_id,
            db=db,
            session_id=session_id,
        )
        data = json.loads(raw)

        if not isinstance(data, dict) or data.get("error"):
            return None

        raw_fields = data.get("fields")
        has_sublists_key = "sublists" in data
        raw_sublists = data.get("sublists")

        # Present-but-wrong-type is "unknown", not "empty".
        if not isinstance(raw_fields, list):
            return None

        # `dict.get()` cannot tell "key absent" from "key present as null", and
        # the two mean opposite things here. An ABSENT "sublists" key is a valid
        # shape for a record with no line items. A key present as null is a
        # malformed response and must degrade to None — writing this as
        # `raw_sublists is not None and not isinstance(...)` silently exempts
        # exactly that case and lets an empty RecordMetadata through, which is
        # the failure this guard exists to prevent.
        if has_sublists_key and not isinstance(raw_sublists, list):
            return None

        line_fields: list[FieldSpec] = []
        for sub in raw_sublists or []:
            if not isinstance(sub, dict):
                return None
            sub_fields = sub.get("fields", [])
            if not isinstance(sub_fields, list):
                return None
            for raw_field in sub_fields:
                if not isinstance(raw_field, dict):
                    return None
                line_fields.append(_parse_field(raw_field))

        fields: list[FieldSpec] = []
        for raw_field in raw_fields:
            if not isinstance(raw_field, dict):
                return None
            fields.append(_parse_field(raw_field))

        meta = RecordMetadata(
            record_type=record_type,
            fields=fields,
            line_fields=line_fields,
        )
    except Exception:
        logger.warning("record_metadata: lookup failed for %s", record_type, exc_info=True)
        return None

    _cache[key] = (time.monotonic(), meta)
    return meta
```

- [ ] **Step 4: Run test to verify it passes**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_record_metadata_service.py -v`
Expected: PASS (9 tests — 3 from Step 1, plus the error-branch, absent-vs-null, and 4 parametrised malformed-shape tests added in fix round 1)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/chat/record_metadata_service.py backend/tests/test_record_metadata_service.py
git commit -m "feat(chat): cached NetSuite record-type metadata service"
```

---

### Task 4: Write validator — required fields, header and line

**Files:**
- Create: `backend/app/services/chat/write_validator.py`
- Test: `backend/tests/test_write_validator.py`

**Interfaces:**
- Consumes: `NormalizedPayload` (Task 1), `RecordMetadata`, `FieldSpec` (Task 3).
- Produces: `EditableSlot(name, label, type, allowed)`, `ValidationResult(ok, unvalidated, missing_required, missing_line_required, invariant_errors, editable_slots, fingerprint())`, `validate_write(payload, metadata, record_type, mutation_type) -> ValidationResult`.

`fingerprint()` is what the repair loop compares across attempts to detect a stall.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_write_validator.py
from app.services.chat.record_metadata_service import FieldSpec, RecordMetadata
from app.services.chat.write_payload import NormalizedPayload
from app.services.chat.write_validator import validate_write

META = RecordMetadata(
    record_type="customer",
    fields=[
        FieldSpec(name="companyname", label="Company Name", required=False),
        FieldSpec(name="subsidiary", label="Primary Subsidiary", required=True,
                  type="select", options=[{"value": "1", "label": "Framework Inc"}]),
    ],
)


def test_missing_required_header_field_is_flagged():
    result = validate_write(
        payload=NormalizedPayload(fields={"companyname": "test ai customer"}, lines=[]),
        metadata=META, record_type="customer", mutation_type="create",
    )
    assert result.ok is False
    assert result.missing_required == ["subsidiary"]
    assert result.editable_slots[0].name == "subsidiary"
    assert result.editable_slots[0].label == "Primary Subsidiary"
    assert result.editable_slots[0].allowed == [{"value": "1", "label": "Framework Inc"}]


def test_complete_payload_passes():
    result = validate_write(
        payload=NormalizedPayload(fields={"companyname": "x", "subsidiary": "1"}, lines=[]),
        metadata=META, record_type="customer", mutation_type="create",
    )
    assert result.ok is True
    assert result.editable_slots == []


def test_missing_line_required_field_is_flagged():
    meta = RecordMetadata(
        record_type="journalEntry",
        fields=[],
        line_fields=[FieldSpec(name="account", label="Account", required=True)],
    )
    result = validate_write(
        payload=NormalizedPayload(fields={}, lines=[{"debit": 5}]),
        metadata=meta, record_type="journalEntry", mutation_type="create",
    )
    assert result.ok is False
    assert "line[0].account" in result.missing_line_required


def test_no_metadata_marks_unvalidated_but_not_invalid():
    result = validate_write(
        payload=NormalizedPayload(fields={"companyname": "x"}, lines=[]),
        metadata=None, record_type="customer", mutation_type="create",
    )
    assert result.unvalidated is True
    assert result.ok is True


def test_update_does_not_require_untouched_fields():
    """A partial update must not demand every required field be resent."""
    result = validate_write(
        payload=NormalizedPayload(fields={"companyname": "renamed"}, lines=[], record_id="7"),
        metadata=META, record_type="customer", mutation_type="update",
    )
    assert result.ok is True


def test_fingerprint_is_stable_and_distinguishing():
    a = validate_write(payload=NormalizedPayload(fields={}, lines=[]), metadata=META,
                       record_type="customer", mutation_type="create")
    b = validate_write(payload=NormalizedPayload(fields={}, lines=[]), metadata=META,
                       record_type="customer", mutation_type="create")
    c = validate_write(payload=NormalizedPayload(fields={"subsidiary": "1"}, lines=[]),
                       metadata=META, record_type="customer", mutation_type="create")
    assert a.fingerprint() == b.fingerprint()
    assert a.fingerprint() != c.fingerprint()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_write_validator.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/chat/write_validator.py
"""Validate a normalized NetSuite write payload before a human ever sees it.

Field requirements come from runtime metadata, never from hardcoded field
names. ``metadata=None`` means requirements are unknown: the payload is marked
``unvalidated`` and allowed through for human review, because blocking every
write during a metadata outage is worse than showing a full payload to a human.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, model_validator

from app.services.chat.record_metadata_service import RecordMetadata
from app.services.chat.write_payload import NormalizedPayload


class EditableSlot(BaseModel):
    name: str
    label: str
    type: str = "text"
    allowed: list[dict[str, Any]] | None = None


class ValidationResult(BaseModel):
    ok: bool
    unvalidated: bool = False
    missing_required: list[str] = []
    missing_line_required: list[str] = []
    invariant_errors: list[str] = []
    editable_slots: list[EditableSlot] = []

    @model_validator(mode="after")
    def _ok_must_agree_with_its_own_lists(self) -> "ValidationResult":
        """`ok` is the flag Task 6's repair loop and Task 9's card gate on.

        It summarises the three lists, so it must never disagree with them.
        Deriving it here means a future caller cannot construct a result that
        claims `ok=True` while carrying missing fields — the failure would be
        silent and would wave an invalid write through to a human as if it
        had been checked.
        """
        derived = not (self.missing_required or self.missing_line_required or self.invariant_errors)
        if self.ok != derived:
            raise ValueError(
                f"ValidationResult.ok={self.ok} disagrees with its contents "
                f"(missing_required={self.missing_required}, "
                f"missing_line_required={self.missing_line_required}, "
                f"invariant_errors={self.invariant_errors})"
            )
        return self

    def fingerprint(self) -> str:
        """Stable identity of *what is wrong*, for stall detection."""
        return "|".join(
            [
                ",".join(sorted(self.missing_required)),
                ",".join(sorted(self.missing_line_required)),
                ",".join(sorted(self.invariant_errors)),
            ]
        )

    def as_model_error(self) -> dict[str, Any]:
        """Structured error handed back to the model instead of a card."""
        return {
            "validation_failed": True,
            "missing_required_fields": self.missing_required,
            "missing_line_fields": self.missing_line_required,
            "invariant_errors": self.invariant_errors,
            "instruction": (
                "Do NOT retry with the same payload. Resolve these fields first "
                "(use ns_getRecordTypeMetadata / ns_getSubsidiaries), then call "
                "the write tool again with a complete payload."
            ),
        }


def validate_write(
    *,
    payload: NormalizedPayload,
    metadata: RecordMetadata | None,
    record_type: str,
    mutation_type: Literal["create", "update", "delete", "upsert"],
    invariant_errors: list[str] | None = None,
) -> ValidationResult:
    invariant_errors = list(invariant_errors or [])

    if metadata is None:
        return ValidationResult(
            ok=not invariant_errors,
            unvalidated=True,
            invariant_errors=invariant_errors,
        )

    missing: list[str] = []
    missing_lines: list[str] = []

    # Only creates must carry every required field. An update legitimately
    # sends a partial payload — demanding the full set would break renames.
    if mutation_type in ("create", "upsert"):
        missing = [n for n in metadata.required_field_names() if n not in payload.fields]
        for idx, line in enumerate(payload.lines):
            for name in metadata.required_line_field_names():
                if name not in line:
                    missing_lines.append(f"line[{idx}].{name}")

    slots: list[EditableSlot] = []
    for name in missing:
        spec = metadata.spec_for(name)
        slots.append(
            EditableSlot(
                name=name,
                label=spec.label if spec else name,
                type=spec.type if spec else "text",
                allowed=spec.options if spec else None,
            )
        )

    return ValidationResult(
        ok=not (missing or missing_lines or invariant_errors),
        unvalidated=False,
        missing_required=missing,
        missing_line_required=missing_lines,
        invariant_errors=invariant_errors,
        editable_slots=slots,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_write_validator.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/chat/write_validator.py backend/tests/test_write_validator.py
git commit -m "feat(chat): metadata-driven write validator with stall fingerprint"
```

---

### Task 5: Posting invariants — period open and balanced journal entries

**Files:**
- Create: `backend/app/services/chat/posting_invariants.py`
- Test: `backend/tests/test_posting_invariants.py`

**Interfaces:**
- Consumes: `NormalizedPayload` (Task 1).
- Produces: `async check_posting_invariants(payload, record_type, mutation_tool_name, tenant_id, actor_id, correlation_id, db, session_id) -> list[str]` — a list of human-readable invariant errors, empty when clean.

These run **regardless of metadata availability**. A metadata outage must not silently disable them.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_posting_invariants.py
import json
import pytest

from app.services.chat import posting_invariants as pi
from app.services.chat.write_payload import NormalizedPayload

EXT = "ext__" + "a" * 32 + "__ns_createRecord"
KW = dict(mutation_tool_name=EXT, tenant_id=None, actor_id=None,
          correlation_id="c", db=None, session_id="s")


@pytest.mark.asyncio
async def test_unbalanced_journal_entry_is_rejected(monkeypatch):
    async def no_period(**kwargs):
        return json.dumps({"items": []})

    monkeypatch.setattr(pi, "execute_tool_call", no_period)
    errors = await pi.check_posting_invariants(
        payload=NormalizedPayload(
            fields={"trandate": "2026-08-19"},
            lines=[{"debit": 100}, {"credit": 60}],
        ),
        record_type="journalEntry", **KW,
    )
    assert any("balance" in e.lower() for e in errors)


@pytest.mark.asyncio
async def test_balanced_journal_entry_passes(monkeypatch):
    async def no_period(**kwargs):
        return json.dumps({"items": []})

    monkeypatch.setattr(pi, "execute_tool_call", no_period)
    errors = await pi.check_posting_invariants(
        payload=NormalizedPayload(fields={}, lines=[{"debit": 100}, {"credit": 100}]),
        record_type="journalEntry", **KW,
    )
    assert errors == []


@pytest.mark.asyncio
async def test_closed_period_is_rejected(monkeypatch):
    async def closed(**kwargs):
        return json.dumps({"items": [{"periodname": "Jul 2026", "closed": True}]})

    monkeypatch.setattr(pi, "execute_tool_call", closed)
    errors = await pi.check_posting_invariants(
        payload=NormalizedPayload(fields={"trandate": "2026-07-15"}, lines=[]),
        record_type="invoice", **KW,
    )
    assert any("period" in e.lower() for e in errors)


@pytest.mark.asyncio
async def test_non_transaction_record_skips_invariants(monkeypatch):
    async def boom(**kwargs):
        raise AssertionError("should not query periods for a customer")

    monkeypatch.setattr(pi, "execute_tool_call", boom)
    errors = await pi.check_posting_invariants(
        payload=NormalizedPayload(fields={"companyname": "x"}, lines=[]),
        record_type="customer", **KW,
    )
    assert errors == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_posting_invariants.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/chat/posting_invariants.py
"""Two posting invariants that record-type metadata cannot express.

Deliberately only two: the accounting period must be open, and a journal entry
must balance. Amount provenance, approval envelopes and posting budgets belong
to the autonomous-accounting-ops program, not here.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.chat.tools import execute_tool_call
from app.services.chat.write_payload import NormalizedPayload

logger = logging.getLogger(__name__)

# Record types that post to the general ledger. Only these get invariant checks.
_TRANSACTION_TYPES: frozenset[str] = frozenset(
    {
        "journalEntry", "invoice", "creditMemo", "customerPayment",
        "customerDeposit", "vendorBill", "vendorPayment", "vendorCredit",
        "expenseReport", "deposit", "check",
    }
)

_BALANCED_TYPES: frozenset[str] = frozenset({"journalEntry"})


def _to_decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal(0)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _check_balanced(payload: NormalizedPayload) -> list[str]:
    debits = sum(_to_decimal(line.get("debit")) for line in payload.lines)
    credits = sum(_to_decimal(line.get("credit")) for line in payload.lines)
    if debits != credits:
        return [
            f"Journal entry does not balance: debits {debits} != credits {credits}."
        ]
    return []


async def _check_period_open(
    payload: NormalizedPayload, **kw: Any
) -> list[str]:
    tran_date = payload.fields.get("trandate") or payload.fields.get("tranDate")
    if not tran_date:
        return []

    from app.services.chat.tools import _make_ext_tool_name, parse_external_tool_name

    parsed = parse_external_tool_name(kw["mutation_tool_name"])
    if not parsed:
        return []

    query = (
        "SELECT periodname, closed FROM accountingperiod "
        f"WHERE startdate <= TO_DATE('{tran_date}', 'YYYY-MM-DD') "
        f"AND enddate >= TO_DATE('{tran_date}', 'YYYY-MM-DD') "
        "AND isquarter = 'F' AND isyear = 'F'"
    )
    # Same guarantee as record_metadata_service: the ENTIRE fetch-and-parse
    # body is guarded, so a malformed-but-valid-JSON response cannot raise out
    # of an invariant check. Note the asymmetry with metadata lookup — here an
    # indeterminate result returns [] (no invariant violation asserted), because
    # fabricating a "period is closed" error from a parse failure would block
    # legitimate writes. The tradeoff is deliberate: we never invent a
    # violation, and a period we could not read is reported by the card's
    # unvalidated marker rather than by a false error here.
    try:
        raw = await execute_tool_call(
            tool_name=_make_ext_tool_name(parsed[0], "ns_runCustomSuiteQL"),
            tool_input={"query": query},
            tenant_id=kw["tenant_id"],
            actor_id=kw["actor_id"],
            correlation_id=kw["correlation_id"],
            db=kw["db"],
            session_id=kw["session_id"],
        )
        data = json.loads(raw)

        if not isinstance(data, dict):
            return []
        rows = data.get("items") or data.get("data") or []
        if not isinstance(rows, list):
            return []

        for row in rows:
            if not isinstance(row, dict):
                continue
            closed = str(row.get("closed", "")).strip().upper()
            if closed in ("T", "TRUE", "YES"):
                name = row.get("periodname", tran_date)
                return [f"Accounting period '{name}' is closed — posting is not permitted."]
    except Exception:
        # Cannot determine period state — do not fabricate a pass or a fail.
        logger.warning("posting_invariants: period lookup failed", exc_info=True)
        return []

    return []


async def check_posting_invariants(
    *,
    payload: NormalizedPayload,
    record_type: str,
    mutation_tool_name: str,
    tenant_id: Any,
    actor_id: Any,
    correlation_id: str,
    db: Any,
    session_id: str,
) -> list[str]:
    """Return invariant violations, empty when clean."""
    if record_type not in _TRANSACTION_TYPES:
        return []

    errors: list[str] = []
    if record_type in _BALANCED_TYPES:
        errors.extend(_check_balanced(payload))

    errors.extend(
        await _check_period_open(
            payload,
            mutation_tool_name=mutation_tool_name,
            tenant_id=tenant_id,
            actor_id=actor_id,
            correlation_id=correlation_id,
            db=db,
            session_id=session_id,
        )
    )
    return errors
```

- [ ] **Step 4: Run test to verify it passes**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_posting_invariants.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/chat/posting_invariants.py backend/tests/test_posting_invariants.py
git commit -m "feat(chat): period-open and journal-balance posting invariants"
```

---

### Task 6: Bounded repair loop in the mutation intercept

**Files:**
- Modify: `backend/app/services/chat/agents/base_agent.py:1234-1291`
- Test: `backend/tests/test_write_repair_loop.py`

**Interfaces:**
- Consumes: everything from Tasks 1, 3, 4, 5.
- Produces: repair state on the agent instance — `self._write_repair_attempts: dict[str, int]` and `self._write_repair_fingerprints: dict[str, str]`, keyed by record_type. Exit reason recorded in `self._write_repair_exit: str | None` with values `done | budget | stall | error`.

**Repair budget: 2 attempts.** A repeated fingerprint exits `stall` immediately rather than consuming the budget.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_write_repair_loop.py
import pytest

from app.services.chat.write_validator import ValidationResult


class _Loop:
    """Exercises the repair-decision helper in isolation."""

    def __init__(self):
        from app.services.chat.agents.base_agent import WriteRepairState
        self.state = WriteRepairState(max_attempts=2)


def test_first_failure_requests_repair():
    from app.services.chat.agents.base_agent import WriteRepairState
    state = WriteRepairState(max_attempts=2)
    result = ValidationResult(ok=False, missing_required=["subsidiary"])
    assert state.should_repair("customer", result) is True
    assert state.exit_reason is None


def test_identical_failure_twice_exits_stall():
    from app.services.chat.agents.base_agent import WriteRepairState
    state = WriteRepairState(max_attempts=2)
    result = ValidationResult(ok=False, missing_required=["subsidiary"])
    state.should_repair("customer", result)
    assert state.should_repair("customer", result) is False
    assert state.exit_reason == "stall"


def test_budget_exhausts_after_max_attempts():
    from app.services.chat.agents.base_agent import WriteRepairState
    state = WriteRepairState(max_attempts=2)
    state.should_repair("customer", ValidationResult(ok=False, missing_required=["a"]))
    state.should_repair("customer", ValidationResult(ok=False, missing_required=["b"]))
    assert state.should_repair("customer", ValidationResult(ok=False, missing_required=["c"])) is False
    assert state.exit_reason == "budget"


def test_success_records_done():
    from app.services.chat.agents.base_agent import WriteRepairState
    state = WriteRepairState(max_attempts=2)
    state.should_repair("customer", ValidationResult(ok=False, missing_required=["a"]))
    assert state.should_repair("customer", ValidationResult(ok=True)) is False
    assert state.exit_reason == "done"


def test_state_is_per_record_type():
    from app.services.chat.agents.base_agent import WriteRepairState
    state = WriteRepairState(max_attempts=2)
    r = ValidationResult(ok=False, missing_required=["subsidiary"])
    state.should_repair("customer", r)
    assert state.should_repair("invoice", r) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_write_repair_loop.py -v`
Expected: FAIL — `ImportError: cannot import name 'WriteRepairState'`

- [ ] **Step 3: Write minimal implementation**

Add near the top of `backend/app/services/chat/agents/base_agent.py`:

```python
class WriteRepairState:
    """Bounded repair budget for write validation, held in run state.

    A cap the model is asked to respect is a request; a counter that persists
    and decrements is a guarantee. Exits carry a reason, never a bare boolean.
    """

    def __init__(self, max_attempts: int = 2) -> None:
        self.max_attempts = max_attempts
        self._attempts: dict[str, int] = {}
        self._fingerprints: dict[str, str] = {}
        self.exit_reason: str | None = None

    def should_repair(self, record_type: str, result: "ValidationResult") -> bool:
        if result.ok:
            self.exit_reason = "done"
            return False

        fingerprint = result.fingerprint()
        if self._fingerprints.get(record_type) == fingerprint:
            # Same failure as last time — recomposing will not help.
            self.exit_reason = "stall"
            return False

        attempts = self._attempts.get(record_type, 0)
        if attempts >= self.max_attempts:
            self.exit_reason = "budget"
            return False

        self._attempts[record_type] = attempts + 1
        self._fingerprints[record_type] = fingerprint
        return True
```

Add the import at the top of the file:

```python
from app.services.chat.write_validator import ValidationResult
```

Then in the mutation intercept, immediately after `mutation_type = classify_mutation(block.name)` and the `if mutation_type is not None:` guard (line 1238), before the existing `current_record` pre-fetch:

```python
                        # ── Write validation + bounded repair ──
                        if not hasattr(self, "_write_repair"):
                            self._write_repair = WriteRepairState(max_attempts=2)

                        validation = None
                        try:
                            normalized = normalize_write_payload(block.input)
                        except PayloadParseError as exc:
                            result_str = json.dumps(
                                {"error": f"Write payload could not be parsed: {exc}"}
                            )
                            normalized = None

                        if normalized is not None:
                            meta = await get_record_metadata(
                                record_type=record_type,
                                mutation_tool_name=block.name,
                                tenant_id=self.tenant_id,
                                actor_id=self.user_id,
                                correlation_id=self.correlation_id,
                                db=db,
                                session_id=session_id or str(self.tenant_id),
                            )
                            invariants = await check_posting_invariants(
                                payload=normalized,
                                record_type=record_type,
                                mutation_tool_name=block.name,
                                tenant_id=self.tenant_id,
                                actor_id=self.user_id,
                                correlation_id=self.correlation_id,
                                db=db,
                                session_id=session_id or str(self.tenant_id),
                            )
                            validation = validate_write(
                                payload=normalized,
                                metadata=meta,
                                record_type=record_type,
                                mutation_type=mutation_type,
                                invariant_errors=invariants,
                            )

                            if self._write_repair.should_repair(record_type, validation):
                                # Hand the model a structured error INSTEAD of a
                                # card. The human never sees an invalid payload.
                                result_str = json.dumps(validation.as_model_error())
                                yield {
                                    "type": "tool_result",
                                    "tool": block.name,
                                    "result": result_str,
                                }
                                continue
```

Add these imports at the top of the file:

```python
from app.services.chat.posting_invariants import check_posting_invariants
from app.services.chat.record_metadata_service import get_record_metadata
from app.services.chat.write_payload import PayloadParseError, normalize_write_payload
from app.services.chat.write_validator import validate_write
```

Stash the result for the next task to consume — do **not** pass `validation=` to
`build_confirmation_payload` yet, that keyword does not exist until Task 7:

```python
                        # Consumed by Task 7 when the card learns about slots.
                        self._last_validation = validation
```

- [ ] **Step 4: Run tests**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_write_repair_loop.py backend/tests/test_mutation_intercept.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/chat/agents/base_agent.py backend/tests/test_write_repair_loop.py
git commit -m "feat(chat): bounded write-repair loop with stall detection and reason enum"
```

---

### Task 7: Editable slots on the card and server-side merge on approve

**Files:**
- Modify: `backend/app/services/chat/write_confirmation_service.py`
- Modify: `backend/app/services/chat/orchestrator.py:1741-1756`
- Test: `backend/tests/test_write_slot_merge.py`

**Interfaces:**
- Consumes: `EditableSlot`, `ValidationResult` (Task 4).
- Produces: `WriteConfirmationPayload` gains `editable_slots: list[EditableSlot] = []` and `unvalidated: bool = False`; `build_confirmation_payload` gains keyword `validation: ValidationResult | None = None`; new `merge_slot_values(structured_output, slot_values, session_id) -> tuple[bool, str, dict, str]` returning `(is_valid, tool_name, merged_tool_input, error)`.

**Security contract:** the client may submit values only for names present in `editable_slots`. Any other key is rejected. A value outside a slot's `allowed` list is rejected. The token is re-minted over the merged payload.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_write_slot_merge.py
from app.services.chat.mutation_guard import generate_confirmation_token
from app.services.chat.write_confirmation_service import (
    _build_payload_json,
    merge_slot_values,
    validate_and_extract_confirmation,
)

SESSION = "11111111-1111-1111-1111-111111111111"
TOOL = "ext__" + "a" * 32 + "__ns_createRecord"


def _pending(slots):
    tool_input = {"recordType": "customer", "data": '{"companyname": "test ai customer"}'}
    return {
        "type": "write_confirmation",
        "mutation_type": "create",
        "record_type": "customer",
        "tool_name": TOOL,
        "tool_input": tool_input,
        "editable_slots": slots,
        "confirmation_token": generate_confirmation_token(
            SESSION, _build_payload_json(TOOL, tool_input)
        ),
        "status": "pending",
    }


SLOTS = [{"name": "subsidiary", "label": "Primary Subsidiary", "type": "select",
          "allowed": [{"value": "1", "label": "Framework Inc"},
                      {"value": "2", "label": "Framework EU"}]}]


def test_declared_slot_value_is_merged():
    ok, tool_name, merged, err = merge_slot_values(_pending(SLOTS), {"subsidiary": "2"}, SESSION)
    assert ok is True
    assert err == ""
    import json
    assert json.loads(merged["data"])["subsidiary"] == "2"
    assert json.loads(merged["data"])["companyname"] == "test ai customer"


def test_undeclared_field_is_rejected():
    ok, _, _, err = merge_slot_values(_pending(SLOTS), {"companyname": "evil"}, SESSION)
    assert ok is False
    assert "not editable" in err


def test_value_outside_allowlist_is_rejected():
    ok, _, _, err = merge_slot_values(_pending(SLOTS), {"subsidiary": "99"}, SESSION)
    assert ok is False
    assert "not an allowed value" in err


def test_merged_payload_gets_a_fresh_token_and_old_one_dies():
    pending = _pending(SLOTS)
    old_token = pending["confirmation_token"]
    ok, tool_name, merged, _ = merge_slot_values(pending, {"subsidiary": "2"}, SESSION)
    assert ok is True
    stale = {"confirmation_token": old_token, "tool_name": tool_name, "tool_input": merged}
    assert validate_and_extract_confirmation(stale, SESSION)[0] is False


def test_no_slots_and_no_values_is_a_passthrough():
    ok, _, merged, err = merge_slot_values(_pending([]), {}, SESSION)
    assert ok is True and err == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_write_slot_merge.py -v`
Expected: FAIL — `ImportError: cannot import name 'merge_slot_values'`

- [ ] **Step 3: Write minimal implementation**

Add to `WriteConfirmationPayload` (after `proposed_lines`):

```python
    editable_slots: list[EditableSlot] = []
    unvalidated: bool = False
```

Change `status` to include the terminal failure state:

```python
    status: Literal["pending", "approved", "rejected", "failed"] = "pending"
```

Add the `validation` keyword to `build_confirmation_payload` and populate the new fields:

```python
def build_confirmation_payload(
    mutation_type: str,
    record_type: str,
    tool_name: str,
    tool_input: dict[str, Any],
    session_id: str,
    current_record: dict[str, Any] | None = None,
    validation: "ValidationResult | None" = None,
) -> WriteConfirmationPayload | None:
```

…and in the return, add:

```python
        editable_slots=list(validation.editable_slots) if validation else [],
        unvalidated=bool(validation.unvalidated) if validation else False,
        unfillable_line_fields=list(validation.missing_line_required) if validation else [],
```

**Line-level fields are validated but NOT fillable** (operator decision, 2026-08-19).
`validate_write` produces `editable_slots` only for missing *header* fields — a
missing line field is reported in `missing_line_required` with no slot, because a
line-level form needs nested UI and a merge path that writes back into the right
line, which is out of scope here.

So a card must never render a form that cannot actually complete the write. Add to
`WriteConfirmationPayload`:

```python
    unfillable_line_fields: list[str] = []
```

Contract for Task 9: when `unfillable_line_fields` is non-empty the card is
**terminal** — it names the missing line fields, renders no slot inputs, and shows
no Approve button, exactly as if it had failed. A half-form the human can fill in
and approve, that then fails at NetSuite anyway, is worse than an honest stop.
Header-only gaps still render the form as designed.

Follow-up ticket for line-level slots: ClickUp 86bbgznjr.

Now wire Task 6's stashed result through. In `base_agent.py`, extend the existing
`build_confirmation_payload(...)` call (line 1277) with:

```python
                            validation=getattr(self, "_last_validation", None),
```

Then append the merge function:

```python
def merge_slot_values(
    structured_output: dict[str, Any],
    slot_values: dict[str, Any],
    session_id: str,
) -> tuple[bool, str, dict[str, Any], str]:
    """Merge human-supplied values for server-declared editable slots.

    The client may only supply values for names the SERVER declared editable,
    and only values inside each slot's ``allowed`` set when one exists. This is
    what stops a manipulated client authoring an ERP write: it can fill declared
    blanks with allowed values and nothing else.

    Returns ``(is_valid, tool_name, merged_tool_input, error)``.
    """
    tool_name: str = structured_output.get("tool_name", "")
    tool_input: dict[str, Any] = dict(structured_output.get("tool_input", {}))
    slots = {s["name"]: s for s in structured_output.get("editable_slots", [])}

    if not slot_values:
        is_valid, name, original = validate_and_extract_confirmation(
            structured_output, session_id
        )
        return is_valid, name, original, "" if is_valid else "invalid token"

    for key, value in slot_values.items():
        if key not in slots:
            return False, tool_name, {}, f"Field '{key}' is not editable."
        allowed = slots[key].get("allowed")
        if allowed:
            permitted = {str(opt.get("value")) for opt in allowed}
            if str(value) not in permitted:
                return False, tool_name, {}, f"'{value}' is not an allowed value for '{key}'."

    try:
        normalized = normalize_write_payload(tool_input)
    except PayloadParseError as exc:
        return False, tool_name, {}, f"stored payload unparseable: {exc}"

    merged_fields = {**normalized.fields, **slot_values}
    merged_input = dict(tool_input)
    # Write back in the shape the tool expects — `data` as a JSON string.
    if "data" in merged_input:
        merged_input["data"] = json.dumps(merged_fields)
    else:
        merged_input["body"] = merged_fields

    return True, tool_name, merged_input, ""
```

Add the import for `EditableSlot` and `ValidationResult` at the top:

```python
from app.services.chat.write_validator import EditableSlot, ValidationResult
```

In `orchestrator.py`, replace the approve branch's validation call (line 1742):

```python
            if _wc_action == "approve":
                _slot_values = write_confirm.get("slot_values") or {}
                is_valid, tool_name, tool_input, _merge_err = merge_slot_values(
                    _so, _slot_values, str(session.id)
                )
                if not is_valid:
                    yield {"type": "error", "error": _merge_err or "Confirmation token is invalid or tampered."}
                    return
```

Add the import in `orchestrator.py` alongside the existing one (line 1722):

```python
                from app.services.chat.write_confirmation_service import merge_slot_values
```

- [ ] **Step 4: Run tests**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_write_slot_merge.py backend/tests/test_write_confirm_orchestrator.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/chat/write_confirmation_service.py backend/app/services/chat/orchestrator.py backend/tests/test_write_slot_merge.py
git commit -m "feat(chat): server-declared editable slots with allowlist-validated merge"
```

---

### Task 8: Terminal failed status (fixes 86bbgnw9g)

**Files:**
- Modify: `backend/app/services/chat/orchestrator.py:1760-1786`
- Test: `backend/tests/test_write_confirm_orchestrator.py`

**Interfaces:**
- Consumes: `status` literal extended in Task 7.
- Produces: a failed write leaves `structured_output["status"] == "failed"` and `structured_output["error"]` carrying the NetSuite message.

- [ ] **Step 1: Write the failing test**

```python
# append to backend/tests/test_write_confirm_orchestrator.py
@pytest.mark.asyncio
async def test_failed_write_is_terminal_not_pending(monkeypatch, db_session, seeded_session):
    """Regression: a failed write reverted to 'pending' forever."""
    async def failing_exec(**kwargs):
        return json.dumps({"success": False, "error": "HTTP 400: Please enter value(s) for: Primary Subsidiary."})

    monkeypatch.setattr("app.services.chat.orchestrator.execute_tool_call", failing_exec)

    events = [e async for e in run_chat_turn(
        db=db_session, session=seeded_session, user_input="",
        write_confirm={"action": "approve", "confirmation_id": str(seeded_session.confirm_id)},
        tenant_id=seeded_session.tenant_id, user_id=seeded_session.user_id,
    )]

    await db_session.refresh(seeded_session.confirm_msg)
    so = seeded_session.confirm_msg.structured_output
    assert so["status"] == "failed"
    assert "Primary Subsidiary" in so["error"]
    assert any("failed" in str(e).lower() for e in events)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_write_confirm_orchestrator.py -k terminal -v`
Expected: FAIL — `assert 'pending' == 'failed'`

- [ ] **Step 3: Write minimal implementation**

Replace lines 1772-1775 of `orchestrator.py`:

```python
                _updated_so = dict(_so)
                if _exec_succeeded:
                    _updated_so["status"] = "approved"
                else:
                    # Terminal. A failed write must never revert to 'pending' —
                    # that stranded the card with no way forward.
                    _updated_so["status"] = "failed"
                    _updated_so["error"] = _confirm_content.replace("The operation failed: ", "")
                _confirm_msg.structured_output = _updated_so
                _wc_flag_modified(_confirm_msg, "structured_output")
```

- [ ] **Step 4: Run tests**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_write_confirm_orchestrator.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/chat/orchestrator.py backend/tests/test_write_confirm_orchestrator.py
git commit -m "fix(chat): failed writes get a terminal status instead of reverting to pending"
```

---

### Task 9: Frontend — render fields, editable slots, failed and unvalidated states

**Files:**
- Modify: `frontend/src/lib/types.ts`
- Modify: `frontend/src/components/chat/write-confirmation-card.tsx`
- Modify: `frontend/src/components/chat/message-list.tsx:1213` — the only call site; its `onConfirm` must accept and forward `slotValues` to the approve request as `write_confirm.slot_values` (the field Task 7's orchestrator branch reads)
- Test: `frontend/src/components/chat/__tests__/write-confirmation-card.test.tsx`

**Interfaces:**
- Consumes: `editable_slots`, `unvalidated`, `proposed_lines`, `status: "failed"`, `error` from Tasks 7-8.
- Produces: `onConfirm` signature becomes `(slotValues: Record<string, string>) => void`.

**Design mock:** the approved visual reference for this task is the mock published alongside this plan. Match its states.

- [ ] **Step 1: Write the failing test**

```tsx
// frontend/src/components/chat/__tests__/write-confirmation-card.test.tsx
import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { WriteConfirmationCard } from "../write-confirmation-card";

const base = {
  type: "write_confirmation" as const,
  mutation_type: "create" as const,
  record_type: "customer",
  record_id: null,
  proposed_fields: { companyname: "test ai customer" },
  proposed_lines: [],
  current_record: null,
  tool_name: "ext__aaa__ns_createRecord",
  tool_input: {},
  confirmation_token: "t",
  editable_slots: [],
  unvalidated: false,
  status: "pending" as const,
};

describe("WriteConfirmationCard", () => {
  it("renders the proposed fields", () => {
    render(<WriteConfirmationCard data={base} onConfirm={vi.fn()} onReject={vi.fn()} />);
    expect(screen.getByText("test ai customer")).toBeInTheDocument();
  });

  it("renders editable slots and blocks approve until filled", () => {
    const data = {
      ...base,
      editable_slots: [
        { name: "subsidiary", label: "Primary Subsidiary", type: "select",
          allowed: [{ value: "1", label: "Framework Inc" }] },
      ],
    };
    render(<WriteConfirmationCard data={data} onConfirm={vi.fn()} onReject={vi.fn()} />);
    expect(screen.getByLabelText("Primary Subsidiary")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /approve/i })).toBeDisabled();
  });

  it("passes filled slot values to onConfirm", () => {
    const onConfirm = vi.fn();
    const data = {
      ...base,
      editable_slots: [
        { name: "subsidiary", label: "Primary Subsidiary", type: "select",
          allowed: [{ value: "1", label: "Framework Inc" }] },
      ],
    };
    render(<WriteConfirmationCard data={data} onConfirm={onConfirm} onReject={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Primary Subsidiary"), { target: { value: "1" } });
    fireEvent.click(screen.getByRole("button", { name: /approve/i }));
    expect(onConfirm).toHaveBeenCalledWith({ subsidiary: "1" });
  });

  it("renders the failed state with the NetSuite error", () => {
    const data = { ...base, status: "failed" as const,
      error: "Please enter value(s) for: Primary Subsidiary." };
    render(<WriteConfirmationCard data={data} onConfirm={vi.fn()} onReject={vi.fn()} />);
    expect(screen.getByText(/Primary Subsidiary/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /approve/i })).not.toBeInTheDocument();
  });

  it("warns when the payload could not be validated", () => {
    render(<WriteConfirmationCard data={{ ...base, unvalidated: true }}
      onConfirm={vi.fn()} onReject={vi.fn()} />);
    expect(screen.getByText(/could not be validated/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/components/chat/__tests__/write-confirmation-card.test.tsx`
Expected: FAIL — no editable-slot inputs, no failed state, `onConfirm` called with no argument

- [ ] **Step 3: Write minimal implementation**

In `frontend/src/lib/types.ts`, extend `WriteConfirmationData`:

```ts
export interface EditableSlot {
  name: string;
  label: string;
  type: string;
  allowed?: { value: string; label: string }[] | null;
}

// add to WriteConfirmationData:
//   proposed_lines?: Record<string, unknown>[];
//   editable_slots?: EditableSlot[];
//   unvalidated?: boolean;
//   error?: string;
// and widen status to: "pending" | "approved" | "rejected" | "failed"
```

In `write-confirmation-card.tsx`, change the prop signature and add slot state:

```tsx
interface WriteConfirmationCardProps {
  data: WriteConfirmationData;
  onConfirm: (slotValues: Record<string, string>) => void;
  onReject: () => void;
  disabled?: boolean;
}
```

Inside the component, after the existing status booleans:

```tsx
  const [slotValues, setSlotValues] = useState<Record<string, string>>({});
  const isFailed = data.status === "failed";
  const slots = data.editable_slots ?? [];
  const allSlotsFilled = slots.every((s) => (slotValues[s.name] ?? "") !== "");
```

Update the status booleans so a failed card is not treated as pending:

```tsx
  const isPending = data.status === "pending";
```

Render the unvalidated banner, the slot form, and the failed error — place these
between the proposed-fields block and the action buttons:

```tsx
      {data.unvalidated && !isFailed && (
        <div className="flex items-start gap-2 rounded-lg border border-amber-400/40 bg-amber-500/[0.04] p-2.5">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-500" />
          <p className="text-[12px] leading-snug text-muted-foreground">
            This payload could not be validated against NetSuite&apos;s field
            requirements. Review every value before approving.
          </p>
        </div>
      )}

      {isFailed && data.error && (
        <div className="rounded-lg border border-red-400/40 bg-red-500/[0.04] p-2.5">
          <p className="text-[12px] font-medium text-red-600 dark:text-red-400">
            NetSuite rejected this write
          </p>
          <p className="mt-1 text-[12px] leading-snug text-muted-foreground">
            {data.error}
          </p>
        </div>
      )}

      {isPending && slots.length > 0 && (
        <div className="space-y-2 rounded-lg border border-amber-400/40 bg-amber-500/[0.04] p-3">
          <p className="text-[11px] font-semibold uppercase tracking-wide text-amber-600 dark:text-amber-400">
            Required — please complete
          </p>
          {slots.map((slot) => (
            <div key={slot.name} className="flex items-center gap-3">
              <label
                htmlFor={`slot-${slot.name}`}
                className="w-40 shrink-0 text-[12px] text-muted-foreground"
              >
                {slot.label}
              </label>
              {slot.allowed && slot.allowed.length > 0 ? (
                <select
                  id={`slot-${slot.name}`}
                  className="h-8 flex-1 rounded-md border bg-background px-2 text-[13px]"
                  value={slotValues[slot.name] ?? ""}
                  onChange={(e) =>
                    setSlotValues((prev) => ({ ...prev, [slot.name]: e.target.value }))
                  }
                >
                  <option value="">Select…</option>
                  {slot.allowed.map((opt) => (
                    <option key={opt.value} value={opt.value}>
                      {opt.label}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  id={`slot-${slot.name}`}
                  type="text"
                  className="h-8 flex-1 rounded-md border bg-background px-2 text-[13px]"
                  value={slotValues[slot.name] ?? ""}
                  onChange={(e) =>
                    setSlotValues((prev) => ({ ...prev, [slot.name]: e.target.value }))
                  }
                />
              )}
            </div>
          ))}
        </div>
      )}
```

Finally, gate and wire the Approve button (replace the existing confirm button):

```tsx
      {isPending && (
        <button
          type="button"
          disabled={disabled || !allSlotsFilled}
          onClick={() => onConfirm(slotValues)}
          className={cn(
            "h-8 rounded-lg px-3 text-[13px] font-medium",
            "bg-primary text-primary-foreground",
            "disabled:opacity-40 disabled:cursor-not-allowed",
          )}
        >
          Approve
        </button>
      )}
```

Add `useState` to the React import, and `EditableSlot` to the types import.

- [ ] **Step 4: Run tests**

Run: `cd frontend && npx vitest run src/components/chat/__tests__/write-confirmation-card.test.tsx`
Expected: PASS (5 tests)

- [ ] **Step 5: Visual acceptance against the mock**

Render the card in the running app and compare each state against the approved mock. Per CLAUDE.md, green tests are not the acceptance gate for visual work — the rendered output is.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/lib/types.ts frontend/src/components/chat/write-confirmation-card.tsx frontend/src/components/chat/__tests__/write-confirmation-card.test.tsx
git commit -m "feat(chat): confirmation card renders fields, editable slots, failed and unvalidated states"
```

---

### Task 10: Teach metadata-first composition in the knowledge profile

**Files:**
- Modify: `backend/app/services/chat/knowledge_profiles/netsuite_writes.yaml`
- Test: `backend/tests/test_knowledge_profiles.py`

The validator is the guarantee; this is the optimization that keeps most writes from needing a repair round at all.

- [ ] **Step 1: Write the failing test**

```python
# append to backend/tests/test_knowledge_profiles.py
def test_write_profile_teaches_metadata_first():
    from app.services.chat.knowledge_profiles.loader import load_all_profiles

    profile = next(p for p in load_all_profiles() if p.profile_id == "netsuite_writes")
    fragment = profile.prompt_fragment
    assert "ns_getRecordTypeMetadata" in fragment
    assert "required" in fragment.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_knowledge_profiles.py -k metadata_first -v`
Expected: FAIL — the fragment mentions neither

- [ ] **Step 3: Write minimal implementation**

Replace the `prompt_fragment` block in `netsuite_writes.yaml`:

```yaml
prompt_fragment: |
  ## NetSuite Write Operations

  You can create and update NetSuite records. ALL writes are intercepted for
  user confirmation.

  BEFORE composing any create payload:
  1. Call ns_getRecordTypeMetadata for the record type to learn which fields
     are REQUIRED in this account. Do not guess — requirements differ per
     account (a OneWorld account requires a subsidiary; a single-entity one
     does not).
  2. Resolve the values those required fields need (ns_getSubsidiaries for
     subsidiary, and so on).
  3. Compose the payload with every required field present.

  A payload missing required fields is rejected by a validator before the user
  ever sees it, and you will be asked to repair it — so composing correctly the
  first time is faster.

  For updates: explain what will change and why. Partial payloads are fine.
  For creates: list the fields you'll set and why.
  After calling a write tool, you'll see "confirmation_required".
  Summarize the proposal and wait for user approval.
  NEVER claim a write succeeded until the user explicitly approves.
```

- [ ] **Step 4: Run tests**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_knowledge_profiles.py backend/tests/test_prompt_tool_sync.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/chat/knowledge_profiles/netsuite_writes.yaml backend/tests/test_knowledge_profiles.py
git commit -m "feat(chat): teach metadata-first composition in the write profile"
```

---

## Final verification

- [ ] Full backend suite: `backend/.venv/bin/python -m pytest backend/tests/ -q`
- [ ] Full frontend suite: `cd frontend && npx vitest run`
- [ ] Lint: `ruff check backend/ && ruff format --check backend/`
- [ ] **Baseline diff** — run the same suites on the merge-base and compare. "No regressions" requires a baseline; green-vs-nothing proves nothing.
- [ ] **Executing end-to-end probe** against a NetSuite **sandbox**: ask chat to create a customer, assert the agent resolves the subsidiary without help, assert the card renders real fields, approve, assert the record lands. **BLOCKED on ClickUp 86bbgnwbf** — every current connector points at production `6738075`. Do not run this against production.
- [ ] **T2 gate:** `Workflow({name:"code-review-multiangle", args:{target:"<PR#>"}})`. Check `status`, `target`, `base`, and `codex_used` before reading findings. Budget for 2+ rounds.
