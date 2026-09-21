"""The Jev client is the single choke point for sending tenant data to TypeSafe.

Every caller goes through ``ask``; the guards tested here (key present, tenant
allow-listed) therefore cannot be bypassed by adding a caller.
"""

import json
import uuid

import httpx
import pytest

from app.core.config import settings
from app.services.typesafe import client as jev

TENANT = uuid.UUID("90fb7ae5-fd4c-4248-8f82-189a474c7523")
QUESTIONS = {
    "kind": {"type": "choice", "instructions": "Which kind?", "criteria": {"a": None, "b": None}},
    "yes": {"type": "noul", "instructions": "Is it?"},
}
OK_BODY = {
    "model": "jev-1.13.0",
    "answers": {
        "kind": {"type": "choice", "choice": "a", "probabilities": {"a": 0.9, "b": 0.1}, "confidence": 0.8},
        "yes": {"type": "noul", "noul": 0.95},
    },
    "usage": {"input_tokens": 120, "output_tokens": 10},
}


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr(settings, "JEV_TENANT_ALLOWLIST", str(TENANT))
    monkeypatch.setattr(settings, "JEV_MODEL", "jev-1.13.0")


def _transport(handler):
    calls = []

    def wrapped(request):
        calls.append(request)
        return handler(request)

    return httpx.MockTransport(wrapped), calls


async def test_no_key_refuses_without_a_network_call(monkeypatch):
    monkeypatch.setattr(settings, "TYPESAFE_API_KEY", "")
    monkeypatch.setattr(settings, "JEV_TENANT_ALLOWLIST", str(TENANT))
    transport, calls = _transport(lambda r: httpx.Response(200, json=OK_BODY))
    with pytest.raises(jev.JevUnavailableError) as exc:
        await jev.ask(TENANT, {"x": "y"}, QUESTIONS, transport=transport)
    assert exc.value.reason == "disabled"
    assert calls == []


async def test_tenant_outside_allowlist_is_refused_without_a_network_call(enabled):
    transport, calls = _transport(lambda r: httpx.Response(200, json=OK_BODY))
    with pytest.raises(jev.JevUnavailableError) as exc:
        await jev.ask(uuid.uuid4(), {"x": "y"}, QUESTIONS, transport=transport)
    assert exc.value.reason == "tenant_not_allowed"
    assert calls == []


async def test_empty_allowlist_denies_every_tenant(enabled, monkeypatch):
    monkeypatch.setattr(settings, "JEV_TENANT_ALLOWLIST", "")
    transport, calls = _transport(lambda r: httpx.Response(200, json=OK_BODY))
    with pytest.raises(jev.JevUnavailableError) as exc:
        await jev.ask(TENANT, {"x": "y"}, QUESTIONS, transport=transport)
    assert exc.value.reason == "tenant_not_allowed"
    assert calls == []


async def test_success_sends_pinned_model_and_returns_typed_answers(enabled):
    transport, calls = _transport(lambda r: httpx.Response(200, json=OK_BODY))
    result = await jev.ask(TENANT, {"x": "y"}, QUESTIONS, transport=transport)
    sent = json.loads(calls[0].content)
    assert calls[0].headers["authorization"] == "Bearer test-key"
    assert sent == {"state": {"x": "y"}, "model": "jev-1.13.0", "questions": QUESTIONS}
    assert result.answers["kind"]["choice"] == "a"
    assert result.answers["yes"]["noul"] == 0.95
    assert result.model == "jev-1.13.0"
    assert result.input_tokens == 120
    assert result.elapsed_ms >= 0


@pytest.mark.parametrize("status", [401, 422, 429, 529])
async def test_http_error_is_a_reasoned_unavailable(enabled, status):
    transport, _ = _transport(lambda r: httpx.Response(status, json={"error": "x"}))
    with pytest.raises(jev.JevUnavailableError) as exc:
        await jev.ask(TENANT, {"x": "y"}, QUESTIONS, transport=transport)
    assert exc.value.reason == f"http_{status}"


async def test_timeout_is_a_reasoned_unavailable(enabled):
    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)

    transport, _ = _transport(boom)
    with pytest.raises(jev.JevUnavailableError) as exc:
        await jev.ask(TENANT, {"x": "y"}, QUESTIONS, transport=transport)
    assert exc.value.reason == "timeout"


@pytest.mark.parametrize(
    "answers",
    [
        {"kind": OK_BODY["answers"]["kind"]},  # an answer is missing
        {**OK_BODY["answers"], "yes": {"type": "choice", "choice": "a"}},  # wrong type for the question
        {**OK_BODY["answers"], "kind": {"type": "choice", "choice": "zzz", "probabilities": {}, "confidence": 1}},
    ],
)
async def test_malformed_answers_are_rejected(enabled, answers):
    transport, _ = _transport(lambda r: httpx.Response(200, json={**OK_BODY, "answers": answers}))
    with pytest.raises(jev.JevUnavailableError) as exc:
        await jev.ask(TENANT, {"x": "y"}, QUESTIONS, transport=transport)
    assert exc.value.reason == "invalid_response"


# ── gate round 1: a 200 with wrong VALUE types must not reach a caller ─────


@pytest.mark.parametrize(
    "bad",
    [
        {"yes": {"type": "noul", "noul": None}},
        {"yes": {"type": "noul", "noul": "0.9"}},
        {"yes": {"type": "noul", "noul": 1.7}},
        {"yes": {"type": "noul", "noul": True}},
        {"kind": {"type": "choice", "choice": "a", "probabilities": {"a": 1.0}}},  # no confidence
        {"kind": {"type": "choice", "choice": "a", "probabilities": {"a": 1.0}, "confidence": "high"}},
        {"kind": {"type": "choice", "choice": "a", "probabilities": [], "confidence": 0.9}},
    ],
)
async def test_wrongly_typed_answer_values_are_rejected(enabled, bad):
    transport, _ = _transport(lambda r: httpx.Response(200, json={**OK_BODY, "answers": {**OK_BODY["answers"], **bad}}))
    with pytest.raises(jev.JevUnavailableError) as exc:
        await jev.ask(TENANT, {"x": "y"}, QUESTIONS, transport=transport)
    assert exc.value.reason == "invalid_response"


async def test_score_answers_must_be_numeric(enabled):
    questions = {"sev": {"type": "score", "instructions": "How bad?", "criteria": ["low", "high"]}}
    body = {**OK_BODY, "answers": {"sev": {"type": "score", "score": None, "probabilities": {}, "confidence": 0.9}}}
    transport, _ = _transport(lambda r: httpx.Response(200, json=body))
    with pytest.raises(jev.JevUnavailableError) as exc:
        await jev.ask(TENANT, {"x": "y"}, questions, transport=transport)
    assert exc.value.reason == "invalid_response"


# ── try_ask: the form every caller uses, which cannot raise ────────────────


async def test_try_ask_returns_the_result_or_a_reason_and_never_raises(enabled, monkeypatch):
    transport, _ = _transport(lambda r: httpx.Response(200, json=OK_BODY))
    result, reason = await jev.try_ask(TENANT, {"x": "y"}, QUESTIONS, transport=transport)
    assert result.answers["yes"]["noul"] == 0.95 and reason is None

    transport, _ = _transport(lambda r: httpx.Response(529))
    assert await jev.try_ask(TENANT, {"x": "y"}, QUESTIONS, transport=transport) == (None, "http_529")

    async def boom(*a, **k):
        raise KeyError("a bug on our side")

    monkeypatch.setattr(jev, "ask", boom)
    assert await jev.try_ask(TENANT, {"x": "y"}, QUESTIONS) == (None, "unexpected:KeyError")


async def test_try_ask_lets_cancellation_through(enabled, monkeypatch):
    import asyncio

    async def cancelled(*a, **k):
        raise asyncio.CancelledError

    monkeypatch.setattr(jev, "ask", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await jev.try_ask(TENANT, {"x": "y"}, QUESTIONS)


# ── session: a batch caller reuses one connection ──────────────────────────


async def test_a_session_reuses_one_http_client_and_closes_it(enabled, monkeypatch):
    built = []
    real = httpx.AsyncClient

    def counting(*args, **kwargs):
        client = real(*args, **kwargs)
        built.append(client)
        return client

    monkeypatch.setattr(jev.httpx, "AsyncClient", counting)
    transport, calls = _transport(lambda r: httpx.Response(200, json=OK_BODY))
    async with jev.session(transport=transport):
        await jev.ask(TENANT, {"x": "y"}, QUESTIONS)
        await jev.ask(TENANT, {"x": "y"}, QUESTIONS)
    assert len(calls) == 2 and len(built) == 1
    assert built[0].is_closed

    await jev.ask(TENANT, {"x": "y"}, QUESTIONS, transport=transport)  # outside a session: its own client
    assert len(built) == 2 and built[1].is_closed


async def test_try_ask_contains_a_failing_request_builder(enabled):
    def broken_builder():
        raise ValueError("bad state")

    assert await jev.try_ask(TENANT, build=broken_builder) == (None, "unexpected:ValueError")


# ── gate round 2: no non-finite or out-of-range number can be returned ─────


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -0.5, 1.5])
async def test_score_must_be_finite_and_within_its_levels(enabled, score):
    questions = {"sev": {"type": "score", "instructions": "How bad?", "criteria": ["low", "high"]}}
    answer = {"type": "score", "score": score, "probabilities": {}, "confidence": 0.9}
    body = json.dumps({**OK_BODY, "answers": {"sev": answer}})  # json.dumps emits NaN/Infinity, as a vendor bug would
    transport, _ = _transport(lambda r: httpx.Response(200, content=body, headers={"content-type": "application/json"}))
    with pytest.raises(jev.JevUnavailableError) as exc:
        await jev.ask(TENANT, {"x": "y"}, questions, transport=transport)
    assert exc.value.reason == "invalid_response"


async def test_a_refused_tenant_never_runs_the_request_builder(enabled):
    built = []
    result = await jev.try_ask(uuid.uuid4(), build=lambda: built.append(1) or ({}, {}))
    assert result == (None, "tenant_not_allowed") and built == []
