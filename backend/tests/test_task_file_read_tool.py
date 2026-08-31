"""`task_file.read` — structured access to an uploaded file's full contents.

WHY IT EXISTS. Chat has accepted .xlsx/.csv/.xls/.json attachments for a while,
but the agent only ever saw a PREVIEW: the first 20 rows x 12 columns
(`_preview_xlsx_attachment`, orchestrator.py). Past that boundary it was
guessing, silently — a 200-row file looked like a 20-row file with no signal
that anything had been cut.

Worse, the `.xls` branch of that preview already told the model, verbatim, to
"use file-aware tools with the file_id below" — and no such general tool
existed. Only `pricing.convert` read a task file, and only for pricing. That is
an asserted affordance, the same class of defect as the `ns_selector_app` dead
end: the model is directed at a door that isn't there. This tool is what makes
that sentence true.

DESIGN CONSTRAINTS, each earned:

* **The page cap is CODE, not prose.** A model that asks for 100_000 rows gets
  _MAX_ROWS. A limit the model is merely asked to respect is a request; a limit
  enforced at the choke point is a guarantee (.claude/rules/agent-graph.md #4).
* **Truncation is always reported.** `total_rows` and `has_more` are returned on
  every call, so a partial read can never be mistaken for a whole one. Silent
  caps read as "I covered everything" when they didn't.
* **Unsupported input fails honestly.** No guessing at a binary blob's contents,
  no empty result that looks like an empty file.
* **Tenant scoping is inherited, not re-implemented** — `TaskFileService.get_file`
  already enforces it and raises for a foreign file.
"""

import io
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp.tools import task_file_tools

_TENANT = uuid.uuid4()
_FILE_ID = uuid.uuid4()


def _csv_bytes(rows: int = 5) -> bytes:
    lines = ["sku,price,currency"]
    lines += [f"SKU-{i},{i * 10}.00,USD" for i in range(1, rows + 1)]
    return ("\n".join(lines)).encode()


def _xlsx_bytes(rows: int = 5) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Prices"
    ws.append(["sku", "price", "currency"])
    for i in range(1, rows + 1):
        ws.append([f"SKU-{i}", i * 10, "USD"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _patch_file(content: bytes, file_type: str, filename: str = "f.csv"):
    task_file = MagicMock()
    task_file.file_type = file_type
    task_file.filename = filename
    return patch.object(
        task_file_tools._file_svc,
        "get_file",
        new=AsyncMock(return_value=(task_file, content)),
    )


async def _run(params):
    """Invoke EXACTLY as the dispatcher does.

    governed_execute calls `execute_fn(validated_params, context=context)`
    (governance.py:518) — positional params, keyword context, db INSIDE context.
    The first version of this helper passed `db=` as its own kwarg, which is not
    a shape any real caller uses; all 17 tests passed while the tool errored on
    every live invocation with "missing 1 required positional argument: 'db'".
    A test harness that invents its own calling convention tests nothing about
    reachability."""
    return await task_file_tools.read_execute({**params}, context={"tenant_id": str(_TENANT), "db": AsyncMock()})


class TestReadsRealContent:
    @pytest.mark.asyncio
    async def test_csv_rows_and_header(self):
        with _patch_file(_csv_bytes(3), "csv"):
            out = await _run({"file_id": str(_FILE_ID)})
        assert out["columns"] == ["sku", "price", "currency"]
        assert out["rows"][0] == ["SKU-1", "10.00", "USD"]
        assert out["total_rows"] == 3

    @pytest.mark.asyncio
    async def test_xlsx_rows_and_sheet_names(self):
        with _patch_file(_xlsx_bytes(3), "xlsx", "f.xlsx"):
            out = await _run({"file_id": str(_FILE_ID)})
        assert out["columns"] == ["sku", "price", "currency"]
        assert out["total_rows"] == 3
        assert out["sheets"] == ["Prices"]

    @pytest.mark.asyncio
    async def test_reads_past_the_twenty_row_preview_boundary(self):
        """The whole point: the preview stopped at 20 rows."""
        with _patch_file(_csv_bytes(50), "csv"):
            out = await _run({"file_id": str(_FILE_ID), "limit": 50})
        assert out["total_rows"] == 50
        assert len(out["rows"]) == 50
        assert out["rows"][-1][0] == "SKU-50"


class TestPagingIsEnforcedInCode:
    @pytest.mark.asyncio
    async def test_an_oversized_limit_is_capped_not_honoured(self):
        """A cap the model is asked to respect is a request. This one is a
        guarantee — it does not matter what the model sends."""
        with _patch_file(_csv_bytes(500), "csv"):
            out = await _run({"file_id": str(_FILE_ID), "limit": 100_000})
        assert len(out["rows"]) == task_file_tools._MAX_ROWS
        assert out["has_more"] is True

    @pytest.mark.asyncio
    async def test_truncation_is_always_reported(self):
        """A partial read must never be mistakable for a whole one."""
        with _patch_file(_csv_bytes(500), "csv"):
            out = await _run({"file_id": str(_FILE_ID), "limit": 10})
        assert out["total_rows"] == 500
        assert len(out["rows"]) == 10
        assert out["has_more"] is True
        assert out["next_offset"] == 10

    @pytest.mark.asyncio
    async def test_offset_pages_forward(self):
        with _patch_file(_csv_bytes(30), "csv"):
            out = await _run({"file_id": str(_FILE_ID), "offset": 25, "limit": 10})
        assert out["rows"][0][0] == "SKU-26"
        assert out["has_more"] is False

    @pytest.mark.asyncio
    async def test_a_complete_read_says_so(self):
        with _patch_file(_csv_bytes(4), "csv"):
            out = await _run({"file_id": str(_FILE_ID)})
        assert out["has_more"] is False


class TestFailsHonestly:
    @pytest.mark.asyncio
    async def test_unsupported_type_is_an_error_not_a_guess(self):
        with _patch_file(b"%PDF-1.7 ...", "pdf", "scan.pdf"):
            out = await _run({"file_id": str(_FILE_ID)})
        assert out.get("error") is True
        assert "pdf" in out["message"].lower()

    @pytest.mark.asyncio
    async def test_missing_file_id_is_an_error(self):
        out = await _run({})
        assert out.get("error") is True

    @pytest.mark.asyncio
    async def test_a_file_the_tenant_does_not_own_surfaces_as_an_error(self):
        """Tenant scoping is TaskFileService's job; this asserts the tool does
        not swallow its refusal into an empty-looking success."""
        with patch.object(task_file_tools._file_svc, "get_file", new=AsyncMock(side_effect=ValueError("not found"))):
            out = await _run({"file_id": str(_FILE_ID)})
        assert out.get("error") is True

    @pytest.mark.asyncio
    async def test_an_empty_file_is_empty_not_broken(self):
        with _patch_file(b"", "csv"):
            out = await _run({"file_id": str(_FILE_ID)})
        assert out.get("error") is not True
        assert out["rows"] == []
        assert out["total_rows"] == 0


class TestLegacyXlsIsNotPretendedToWork:
    """The tool was written to stop the app pointing the model at affordances
    that do not exist — and then claimed .xls support it does not have.
    openpyxl reads .xlsx (a zip) and raises BadZipFile on legacy binary .xls;
    xlrd is not a dependency. Upload accepts .xls, so a user CAN get here.

    Honest failure with a fix the user can act on beats a BadZipFile."""

    @pytest.mark.asyncio
    async def test_xls_is_refused_by_name_with_a_remedy(self):
        with _patch_file(b"\xd0\xcf\x11\xe0legacy-ole2", "xls", "old.xls"):
            out = await _run({"file_id": str(_FILE_ID)})
        assert out.get("error") is True
        msg = out["message"].lower()
        assert "xls" in msg
        assert "xlsx" in msg, "must tell the user what to do, not just what failed"

    def test_xls_is_not_advertised_as_supported(self):
        assert "xls" not in task_file_tools._SUPPORTED


class TestWiring:
    @pytest.mark.asyncio
    async def test_callable_the_way_the_dispatcher_actually_calls_it(self):
        """THE regression this file exists for. governed_execute does
        `execute_fn(params, context=context)` — nothing else. If the signature
        drifts from that, every live call dies and no amount of direct-call
        unit testing notices."""
        import inspect

        from app.mcp.registry import TOOL_REGISTRY

        fn = TOOL_REGISTRY["task_file.read"]["execute"]
        with _patch_file(_csv_bytes(2), "csv"):
            # Positional params + keyword context, exactly as governance.py:518.
            out = await fn({"file_id": str(_FILE_ID)}, context={"tenant_id": str(_TENANT), "db": AsyncMock()})
        assert out.get("error") is not True, out
        assert out["total_rows"] == 2
        # And it must tolerate the extra kwargs a dispatcher may add later.
        sig = inspect.signature(fn)
        assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()), (
            "must accept **kwargs — the dispatcher is free to pass more than params/context"
        )

    @pytest.mark.asyncio
    async def test_missing_db_in_context_is_an_honest_error(self):
        out = await task_file_tools.read_execute({"file_id": str(_FILE_ID)}, context={"tenant_id": str(_TENANT)})
        assert out.get("error") is True

    def test_registered_and_exposed_to_chat(self):
        from app.mcp.registry import TOOL_REGISTRY
        from app.services.chat.nodes import ALLOWED_CHAT_TOOLS

        assert "task_file.read" in TOOL_REGISTRY
        assert "task_file.read" in ALLOWED_CHAT_TOOLS

    def test_json_files_are_supported(self):
        assert "json" in task_file_tools._SUPPORTED

    @pytest.mark.asyncio
    async def test_json_array_reads_as_rows(self):
        payload = json.dumps([{"sku": "A", "price": 1}, {"sku": "B", "price": 2}]).encode()
        with _patch_file(payload, "json", "f.json"):
            out = await _run({"file_id": str(_FILE_ID)})
        assert out["columns"] == ["sku", "price"]
        assert out["total_rows"] == 2
