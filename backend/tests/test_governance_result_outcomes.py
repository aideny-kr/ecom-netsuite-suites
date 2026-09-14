from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.mcp import governance


@pytest.mark.parametrize(
    "result, expected",
    [
        ({"success": False, "error": "invalid_parameters"}, "error"),
        ({"error": "unavailable"}, "error"),
        ({"isError": True, "content": []}, "error"),
        ({"success": True, "rows": []}, "success"),
        ({"rows": [{"error": "historical finding"}]}, "success"),
    ],
)
async def test_returned_tool_failure_is_not_a_successful_audit(monkeypatch, result, expected):
    monkeypatch.setattr(governance, "check_rate_limit", lambda *args: True)
    audit = AsyncMock()
    monkeypatch.setattr(governance.audit_service, "log_event", audit)
    metrics = []
    monkeypatch.setattr(governance, "record_call", lambda name, status: metrics.append(status))
    returned = await governance.governed_execute(
        "transaction_ops.status",
        {"case_id": str(uuid4())},
        str(uuid4()),
        str(uuid4()),
        AsyncMock(return_value=result),
        db=object(),
        actor_type="system",
    )
    assert returned == governance.redact_result(result)
    assert metrics == [expected]
    events = [call.kwargs for call in audit.await_args_list]
    assert events[0]["action"] == "tool.requested"
    assert events[-1]["action"] == ("tool.failed" if expected == "error" else "tool.executed")
    assert events[-1]["status"] == expected
    assert events[-1]["actor_type"] == "system"
