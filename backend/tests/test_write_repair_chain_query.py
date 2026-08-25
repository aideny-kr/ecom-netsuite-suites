"""Tests for orchestrator._resolve_repair_chain_previous_fingerprint — the
DB-touching half of the bounded write-repair loop's stall detection
(requirement D). Reads the PERSISTED chain (root card + its repair cards,
scoped to the session), never an in-memory object, so the bound survives a
process restart / new session / /clear per the bounding ruling.

A minimal, purpose-built db.execute mock is used here (rather than
test_write_confirm_orchestrator.py's `_make_db`, which always returns the
SAME confirm_msg regardless of query shape) so this file can assert on
DIFFERENT rows being returned for different (root_id, target_attempt)
lookups — exactly what the bound gate depends on.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.chat import ChatMessage
from app.services.chat.orchestrator import _resolve_repair_chain_previous_fingerprint

_SESSION_ID = uuid.uuid4()
_ROOT_ID = uuid.uuid4()


def _row(repair_attempt: int, failure_fingerprint: str | None) -> ChatMessage:
    msg = MagicMock(spec=ChatMessage)
    msg.structured_output = {"repair_attempt": repair_attempt, "failure_fingerprint": failure_fingerprint}
    return msg


class _ScalarResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


def _make_db(row):
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ScalarResult(row))
    return db


class TestResolveRepairChainPreviousFingerprint:
    @pytest.mark.asyncio
    async def test_negative_target_attempt_short_circuits_without_a_query(self):
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=AssertionError("must not query for a negative target_attempt"))

        result = await _resolve_repair_chain_previous_fingerprint(db, _SESSION_ID, str(_ROOT_ID), -1)
        assert result is None
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_root_id_returns_none_without_raising(self):
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=AssertionError("must not query with an unparseable root_id"))

        result = await _resolve_repair_chain_previous_fingerprint(db, _SESSION_ID, "not-a-uuid", 0)
        assert result is None

    @pytest.mark.asyncio
    async def test_matching_row_returns_its_fingerprint(self):
        db = _make_db(_row(1, "fp-abc"))
        result = await _resolve_repair_chain_previous_fingerprint(db, _SESSION_ID, str(_ROOT_ID), 1)
        assert result == "fp-abc"

    @pytest.mark.asyncio
    async def test_no_matching_row_returns_none(self):
        db = _make_db(None)
        result = await _resolve_repair_chain_previous_fingerprint(db, _SESSION_ID, str(_ROOT_ID), 3)
        assert result is None

    @pytest.mark.asyncio
    async def test_row_with_no_failure_fingerprint_key_returns_none(self):
        """The root's own card at attempt 0 exists but never failed (this
        query only runs when SOMETHING failed, but defensively: a row
        without the key must not raise)."""
        msg = MagicMock(spec=ChatMessage)
        msg.structured_output = {"repair_attempt": 0}
        db = _make_db(msg)
        result = await _resolve_repair_chain_previous_fingerprint(db, _SESSION_ID, str(_ROOT_ID), 0)
        assert result is None

    @pytest.mark.asyncio
    async def test_non_dict_structured_output_returns_none_not_raise(self):
        msg = MagicMock(spec=ChatMessage)
        msg.structured_output = None
        db = _make_db(msg)
        result = await _resolve_repair_chain_previous_fingerprint(db, _SESSION_ID, str(_ROOT_ID), 0)
        assert result is None
