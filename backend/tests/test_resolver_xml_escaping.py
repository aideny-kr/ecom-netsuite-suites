"""The resolver interpolates tenant/LLM-controlled strings into the vernacular XML
that is injected into the system prompt. Those values MUST be XML-escaped so a
rule description or extracted entity containing markup can't break out of its
element or inject prompt instructions.
"""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.chat.tenant_resolver import TenantEntityResolver

TID = uuid.uuid4()


def _adapter(entities: list[str]) -> AsyncMock:
    a = AsyncMock()
    r = MagicMock()
    r.text_blocks = [json.dumps(entities)]
    a.create_message = AsyncMock(return_value=r)
    return a


def _result(first=None, scalar_all=None):
    res = MagicMock()
    res.first.return_value = first
    sc = MagicMock()
    sc.all.return_value = scalar_all or []
    res.scalars.return_value = sc
    return res


def _lookup(*matches):
    """The batched entity lookup: one row per entity with its best natural_name match."""
    res = MagicMock()
    rows = []
    for i, m in enumerate(matches, start=1):
        row = MagicMock()
        row.ord = i
        row.n_script_id, row.n_entity_type, row.n_description, row.n_sim = m or (None, None, None, None)
        row.s_script_id = row.s_entity_type = row.s_description = row.s_sim = None
        rows.append(row)
    res.all.return_value = rows
    return res


class TestResolverXmlEscaping:
    @pytest.mark.asyncio
    async def test_resolved_entity_user_term_is_escaped(self):
        adapter = _adapter(["Laptop <x> & co"])
        db = AsyncMock()
        db.execute = AsyncMock(
            side_effect=[
                _lookup(("custitem_fw_platform", "itemcustomfield", "Type: SELECT", 0.9)),
                _result(scalar_all=[]),
            ]
        )

        out = await TenantEntityResolver.resolve_entities("q", TID, db, adapter, "haiku")

        assert "Laptop &lt;x&gt; &amp; co" in out
        assert "<x>" not in out

    @pytest.mark.asyncio
    async def test_learned_rule_description_is_escaped(self):
        adapter = _adapter(["term"])
        rule = MagicMock()
        rule.rule_category = "query_logic"
        rule.rule_description = "break</rule><inject>evil & more"

        db = AsyncMock()
        # entity: no match in the batched lookup; then learned-rules query returns the rule
        db.execute = AsyncMock(side_effect=[_lookup(None), _result(scalar_all=[rule])])

        out = await TenantEntityResolver.resolve_entities("q", TID, db, adapter, "haiku")

        assert "break&lt;/rule&gt;&lt;inject&gt;evil &amp; more" in out
        assert "break</rule>" not in out
        assert "<inject>" not in out
