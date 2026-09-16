"""G1(e): the system-record deny-list compares case-normalized.

The model supplies ``recordType`` verbatim. NetSuite REST record types are camelCase and
the deny-list is lowercase, so ``Employee`` or ``ACCOUNT`` used to pass an exact-string
``not in`` check. A repair that only changed casing could route around the list.
"""

import pytest

from app.services.chat.mutation_guard import _BLOCKED_RECORD_TYPES, is_record_type_allowed


@pytest.mark.parametrize("blocked", sorted(_BLOCKED_RECORD_TYPES))
@pytest.mark.parametrize("variant", [str.lower, str.upper, str.title, lambda s: f"  {s}  "])
def test_every_blocked_type_is_refused_in_any_casing_or_padding(blocked, variant):
    assert is_record_type_allowed(variant(blocked)) is False


@pytest.mark.parametrize("allowed", ["customer", "Customer", "CUSTOMER", "creditMemo", "salesOrder", "invoice"])
def test_ordinary_record_types_stay_allowed(allowed):
    assert is_record_type_allowed(allowed) is True


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_missing_record_type_is_not_allowed(empty):
    assert is_record_type_allowed(empty) is False


async def test_the_dispatcher_refuses_a_blocked_type_even_with_human_approval():
    """The list is enforced where the write leaves, not only where the card was minted: a
    card minted before the comparison became case-insensitive still cannot be dispatched."""
    import json
    from unittest.mock import AsyncMock, patch
    from uuid import uuid4

    from app.services.chat import tools as tools_mod

    async def call(record_type):
        return await tools_mod.execute_tool_call(
            tool_name=f"ext__{'a' * 32}__ns_updateRecord",
            tool_input={"recordType": record_type, "recordId": "1", "data": "{}"},
            tenant_id=uuid4(),
            actor_id=uuid4(),
            correlation_id="c",
            db=None,
            session_id="s",
            human_approved=True,
        )

    spy = AsyncMock(return_value={"success": True, "recordId": "1"})
    with patch.object(tools_mod, "_execute_external_tool", spy):
        for blocked in ("Employee", "ACCOUNT", " customRecordType ", None):
            out = json.loads(await call(blocked))
            assert out.get("blocked_record_type") is True, blocked
        spy.assert_not_awaited()
        assert "recordId" in await call("Customer")  # ordinary types still dispatch once approved
    spy.assert_awaited_once()
