"""The grouped-errors read path carries the advisory triage label only in live mode."""

from app.core.config import settings
from app.services.celigo import read_queries, triage_jev
from app.services.typesafe.client import JevResult
from tests.api.test_celigo_read_queries_parity import _seed_world


async def _groups(db, admin_user, monkeypatch, mode):
    user, _ = admin_user
    world = await _seed_world(db, user.tenant_id)
    sent = []

    async def fake_ask(tenant_id, state, questions, **_):
        sent.append(state)
        answers = {
            q: {"type": "choice", "choice": "invalid_data", "confidence": 0.9, "probabilities": {}} for q in questions
        }
        return JevResult(answers=answers, model="jev-1.13.0", input_tokens=300, elapsed_ms=100)

    triage_jev.clear_cache()
    monkeypatch.setattr(triage_jev, "ask", fake_ask)
    monkeypatch.setattr(settings, "JEV_CELIGO_TRIAGE_MODE", mode)
    out = await read_queries.flow_error_groups(db, tenant_id=user.tenant_id, flow_id=world["flow"].id, status="open")
    triage_jev.clear_cache()
    return out, sent


async def test_live_attaches_the_label_to_each_group(db, admin_user, monkeypatch):
    out, sent = await _groups(db, admin_user, monkeypatch, "live")
    labelled = [g for g in out.groups if g.signature is not None]
    assert labelled and all(
        g.triage == {"category": "invalid_data", "confidence": 0.9, "advisory": True} for g in labelled
    )
    assert len(sent) == 1


async def test_off_and_shadow_attach_nothing(db, admin_user, monkeypatch):
    for mode in ("off", "shadow"):
        out, _ = await _groups(db, admin_user, monkeypatch, mode)
        assert all(g.triage is None for g in out.groups)
