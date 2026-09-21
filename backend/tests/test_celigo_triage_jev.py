"""Celigo error-signature triage: what KIND of failure is this, judged from a PII-folded message."""

import uuid

import pytest

from app.core.config import settings
from app.services.celigo import triage_jev as tj
from app.services.typesafe.client import JevResult, JevUnavailableError

TENANT = uuid.uuid4()
SIGS = [
    {
        "fingerprint": "fp-auth",
        "source": "connection",
        "code": "401",
        "sample_message": "Token expired for jane.doe@example.com at 2026-09-01T10:00:00Z",
    },
    {
        "fingerprint": "fp-data",
        "source": "import",
        "code": "INVALID_KEY_OR_REF",
        "sample_message": "Invalid item reference key 88231 for order R628489275",
    },
]


def _choice(option, confidence):
    return {"type": "choice", "choice": option, "confidence": confidence, "probabilities": {}}


def _patch(monkeypatch, answers=None, error=None):
    calls = []

    async def fake_ask(tenant_id, state, questions, **_):
        calls.append({"state": state, "questions": questions})
        if error:
            raise error
        return JevResult(answers=answers, model="jev-1.13.0", input_tokens=600, elapsed_ms=130)

    monkeypatch.setattr(tj, "ask", fake_ask)
    return calls


@pytest.fixture(autouse=True)
def _fresh_cache():
    tj.clear_cache()
    yield
    tj.clear_cache()


async def test_off_returns_nothing_and_sends_nothing(monkeypatch):
    monkeypatch.setattr(settings, "JEV_CELIGO_TRIAGE_MODE", "off")
    calls = _patch(monkeypatch, answers={})
    assert await tj.triage_signatures(TENANT, SIGS) == ({}, None)
    assert calls == []


async def test_only_the_pii_folded_message_leaves(monkeypatch):
    monkeypatch.setattr(settings, "JEV_CELIGO_TRIAGE_MODE", "live")
    calls = _patch(monkeypatch, answers={"s0": _choice("authentication", 0.95), "s1": _choice("bad_reference", 0.9)})
    await tj.triage_signatures(TENANT, SIGS)
    sent = str(calls[0]["state"])
    for secret in ("jane.doe@example.com", "R628489275", "88231", "2026-09-01"):
        assert secret not in sent
    assert "<EMAIL>" in sent and "<REF>" in sent
    assert set(calls[0]["questions"]["s0"]["criteria"]) == set(tj.CATEGORIES)


async def test_live_returns_a_category_per_fingerprint(monkeypatch):
    monkeypatch.setattr(settings, "JEV_CELIGO_TRIAGE_MODE", "live")
    _patch(monkeypatch, answers={"s0": _choice("authentication", 0.95), "s1": _choice("bad_reference", 0.9)})
    triage, record = await tj.triage_signatures(TENANT, SIGS)
    assert triage["fp-auth"] == {"category": "authentication", "confidence": 0.95, "advisory": True}
    assert triage["fp-data"]["category"] == "bad_reference"
    assert record["decided_by"] == "jev" and record["signatures"] == 2


async def test_an_unsure_answer_is_reported_as_unclear_not_as_a_guess(monkeypatch):
    monkeypatch.setattr(settings, "JEV_CELIGO_TRIAGE_MODE", "live")
    _patch(monkeypatch, answers={"s0": _choice("authentication", 0.4), "s1": _choice("bad_reference", 0.9)})
    triage, _ = await tj.triage_signatures(TENANT, SIGS)
    assert triage["fp-auth"]["category"] == "unclear"


async def test_shadow_returns_nothing_to_show_but_records_the_answers(monkeypatch):
    monkeypatch.setattr(settings, "JEV_CELIGO_TRIAGE_MODE", "shadow")
    _patch(monkeypatch, answers={"s0": _choice("authentication", 0.95), "s1": _choice("bad_reference", 0.9)})
    triage, record = await tj.triage_signatures(TENANT, SIGS)
    assert triage == {}
    assert record["categories"] == {"authentication": 1, "bad_reference": 1}
    assert "jane" not in str(record) and "Invalid item" not in str(record)


async def test_a_signature_is_judged_once_then_served_from_cache(monkeypatch):
    monkeypatch.setattr(settings, "JEV_CELIGO_TRIAGE_MODE", "live")
    calls = _patch(monkeypatch, answers={"s0": _choice("authentication", 0.95), "s1": _choice("bad_reference", 0.9)})
    await tj.triage_signatures(TENANT, SIGS)
    triage, record = await tj.triage_signatures(TENANT, SIGS)
    assert len(calls) == 1 and record["cached"] == 2
    assert triage["fp-auth"]["category"] == "authentication"


async def test_cache_is_per_tenant(monkeypatch):
    monkeypatch.setattr(settings, "JEV_CELIGO_TRIAGE_MODE", "live")
    calls = _patch(monkeypatch, answers={"s0": _choice("authentication", 0.95), "s1": _choice("bad_reference", 0.9)})
    await tj.triage_signatures(TENANT, SIGS)
    await tj.triage_signatures(uuid.uuid4(), SIGS)
    assert len(calls) == 2


async def test_outage_shows_no_triage(monkeypatch):
    monkeypatch.setattr(settings, "JEV_CELIGO_TRIAGE_MODE", "live")
    _patch(monkeypatch, error=JevUnavailableError("http_529"))
    triage, record = await tj.triage_signatures(TENANT, SIGS)
    assert triage == {} and record["jev_error"] == "http_529"
