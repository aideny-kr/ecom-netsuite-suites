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
