"""The Jev client is the single choke point for sending tenant data to TypeSafe.

Every caller goes through ``ask`` with the key ``typesafe.access.resolve_access`` chose for
the tenant; the client never reads a key from settings, so no caller can send one tenant's
work under another key, and a missing key refuses before any network I/O.
"""

import json
import uuid

import httpx
import pytest

from app.core.config import settings
from app.services.typesafe import client as jev

TENANT = uuid.UUID("90fb7ae5-fd4c-4248-8f82-189a474c7523")
KEY = "tenant-key-1234"
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
    # A different platform key proves the client sends the key it is GIVEN.
    monkeypatch.setattr(settings, "TYPESAFE_API_KEY", "platform-key-must-not-be-sent")
    monkeypatch.setattr(settings, "JEV_MODEL", "jev-1.13.0")


def _transport(handler):
    calls = []

    def wrapped(request):
        calls.append(request)
        return handler(request)

    return httpx.MockTransport(wrapped), calls


@pytest.mark.parametrize("key", ["", None])
async def test_no_key_refuses_without_a_network_call(enabled, key):
    transport, calls = _transport(lambda r: httpx.Response(200, json=OK_BODY))
    with pytest.raises(jev.JevUnavailableError) as exc:
        await jev.ask(TENANT, {"x": "y"}, QUESTIONS, api_key=key, transport=transport)
    assert exc.value.reason == "disabled"
    assert calls == []


async def test_success_sends_pinned_model_and_returns_typed_answers(enabled):
    transport, calls = _transport(lambda r: httpx.Response(200, json=OK_BODY))
    result = await jev.ask(TENANT, {"x": "y"}, QUESTIONS, transport=transport, api_key=KEY)
    sent = json.loads(calls[0].content)
    assert calls[0].headers["authorization"] == f"Bearer {KEY}"
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
        await jev.ask(TENANT, {"x": "y"}, QUESTIONS, transport=transport, api_key=KEY)
    assert exc.value.reason == f"http_{status}"


async def test_timeout_is_a_reasoned_unavailable(enabled):
    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)

    transport, _ = _transport(boom)
    with pytest.raises(jev.JevUnavailableError) as exc:
        await jev.ask(TENANT, {"x": "y"}, QUESTIONS, transport=transport, api_key=KEY)
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
        await jev.ask(TENANT, {"x": "y"}, QUESTIONS, transport=transport, api_key=KEY)
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
        await jev.ask(TENANT, {"x": "y"}, QUESTIONS, transport=transport, api_key=KEY)
    assert exc.value.reason == "invalid_response"


async def test_score_answers_must_be_numeric(enabled):
    questions = {"sev": {"type": "score", "instructions": "How bad?", "criteria": ["low", "high"]}}
    body = {**OK_BODY, "answers": {"sev": {"type": "score", "score": None, "probabilities": {}, "confidence": 0.9}}}
    transport, _ = _transport(lambda r: httpx.Response(200, json=body))
    with pytest.raises(jev.JevUnavailableError) as exc:
        await jev.ask(TENANT, {"x": "y"}, questions, transport=transport, api_key=KEY)
    assert exc.value.reason == "invalid_response"


# ── try_ask: the form every caller uses, which cannot raise ────────────────


async def test_try_ask_returns_the_result_or_a_reason_and_never_raises(enabled, monkeypatch):
    transport, _ = _transport(lambda r: httpx.Response(200, json=OK_BODY))
    result, reason = await jev.try_ask(TENANT, {"x": "y"}, QUESTIONS, transport=transport, api_key=KEY)
    assert result.answers["yes"]["noul"] == 0.95 and reason is None

    transport, _ = _transport(lambda r: httpx.Response(529))
    assert await jev.try_ask(TENANT, {"x": "y"}, QUESTIONS, transport=transport, api_key=KEY) == (None, "http_529")

    async def boom(*a, **k):
        raise KeyError("a bug on our side")

    monkeypatch.setattr(jev, "ask", boom)
    assert await jev.try_ask(TENANT, {"x": "y"}, QUESTIONS, api_key=KEY) == (None, "unexpected:KeyError")


async def test_try_ask_lets_cancellation_through(enabled, monkeypatch):
    import asyncio

    async def cancelled(*a, **k):
        raise asyncio.CancelledError

    monkeypatch.setattr(jev, "ask", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await jev.try_ask(TENANT, {"x": "y"}, QUESTIONS, api_key=KEY)


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
        await jev.ask(TENANT, {"x": "y"}, QUESTIONS, api_key=KEY)
        await jev.ask(TENANT, {"x": "y"}, QUESTIONS, api_key=KEY)
    assert len(calls) == 2 and len(built) == 1
    assert built[0].is_closed

    await jev.ask(TENANT, {"x": "y"}, QUESTIONS, transport=transport, api_key=KEY)  # outside a session: its own client
    assert len(built) == 2 and built[1].is_closed


async def test_try_ask_contains_a_failing_request_builder(enabled):
    def broken_builder():
        raise ValueError("bad state")

    assert await jev.try_ask(TENANT, build=broken_builder, api_key=KEY) == (None, "unexpected:ValueError")


# ── gate round 2: no non-finite or out-of-range number can be returned ─────


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -0.5, 1.5])
async def test_score_must_be_finite_and_within_its_levels(enabled, score):
    questions = {"sev": {"type": "score", "instructions": "How bad?", "criteria": ["low", "high"]}}
    answer = {"type": "score", "score": score, "probabilities": {}, "confidence": 0.9}
    body = json.dumps({**OK_BODY, "answers": {"sev": answer}})  # json.dumps emits NaN/Infinity, as a vendor bug would
    transport, _ = _transport(lambda r: httpx.Response(200, content=body, headers={"content-type": "application/json"}))
    with pytest.raises(jev.JevUnavailableError) as exc:
        await jev.ask(TENANT, {"x": "y"}, questions, transport=transport, api_key=KEY)
    assert exc.value.reason == "invalid_response"


async def test_without_a_key_the_request_builder_never_runs(enabled):
    built = []
    result = await jev.try_ask(TENANT, build=lambda: built.append(1) or ({}, {}), api_key="")
    assert result == (None, "disabled") and built == []


# ── gate round 3 ───────────────────────────────────────────────────────────


async def test_the_timeout_bounds_the_whole_request_not_each_read(enabled, monkeypatch):
    import asyncio

    monkeypatch.setattr(settings, "JEV_TIMEOUT_SECONDS", 0.05)

    async def trickle(request):
        await asyncio.sleep(0.5)  # a slow body: no single read "times out", the whole call must
        return httpx.Response(200, json=OK_BODY)

    with pytest.raises(jev.JevUnavailableError) as exc:
        await jev.ask(TENANT, {"x": "y"}, QUESTIONS, transport=httpx.MockTransport(trickle), api_key=KEY)
    assert exc.value.reason == "timeout"


@pytest.mark.parametrize("probabilities", [{"a": float("nan"), "b": 0.1}, {"a": 1.4, "b": 0.1}, {"a": "0.9"}])
async def test_every_probability_is_a_finite_number_in_range(enabled, probabilities):
    answer = {"type": "choice", "choice": "a", "probabilities": probabilities, "confidence": 0.8}
    body = json.dumps({**OK_BODY, "answers": {**OK_BODY["answers"], "kind": answer}})
    transport, _ = _transport(lambda r: httpx.Response(200, content=body, headers={"content-type": "application/json"}))
    with pytest.raises(jev.JevUnavailableError) as exc:
        await jev.ask(TENANT, {"x": "y"}, QUESTIONS, transport=transport, api_key=KEY)
    assert exc.value.reason == "invalid_response"


# ── codex cross-examination: a choice's distribution must be coherent ──────


@pytest.mark.parametrize(
    "probabilities",
    [{}, {"a": 1.0}, {"a": 0.9, "b": 0.1, "zzz": 0.0}, {"a": 0.9, "b": 0.5}],
)
async def test_choice_probabilities_must_cover_exactly_the_criteria_and_sum_to_one(enabled, probabilities):
    answer = {"type": "choice", "choice": "a", "probabilities": probabilities, "confidence": 0.8}
    transport, _ = _transport(
        lambda r: httpx.Response(200, json={**OK_BODY, "answers": {**OK_BODY["answers"], "kind": answer}})
    )
    with pytest.raises(jev.JevUnavailableError) as exc:
        await jev.ask(TENANT, {"x": "y"}, QUESTIONS, transport=transport, api_key=KEY)
    assert exc.value.reason == "invalid_response"


async def test_the_chosen_option_must_be_the_most_probable(enabled):
    answer = {"type": "choice", "choice": "a", "probabilities": {"a": 0.2, "b": 0.8}, "confidence": 0.6}
    transport, _ = _transport(
        lambda r: httpx.Response(200, json={**OK_BODY, "answers": {**OK_BODY["answers"], "kind": answer}})
    )
    with pytest.raises(jev.JevUnavailableError):
        await jev.ask(TENANT, {"x": "y"}, QUESTIONS, transport=transport, api_key=KEY)


# ── check_key: what the Jev card's Test button and key save use ────────────


async def test_check_key_accepts_a_working_key(enabled):
    answer = {"type": "choice", "choice": "yes", "probabilities": {"yes": 1.0, "no": 0.0}, "confidence": 1.0}
    transport, calls = _transport(
        lambda r: httpx.Response(200, json={"model": "jev-1.13.0", "answers": {"ready": answer}})
    )
    assert await jev.check_key(KEY, transport=transport) is None
    assert calls[0].headers["authorization"] == f"Bearer {KEY}"
    assert "tenant" not in calls[0].content.decode().lower()  # a probe carries no tenant data


@pytest.mark.parametrize("status", [401, 403, 529])
async def test_check_key_reports_why_a_key_failed(enabled, status):
    transport, _ = _transport(lambda r: httpx.Response(status))
    assert await jev.check_key(KEY, transport=transport) == f"http_{status}"


async def test_check_key_without_a_key_makes_no_call(enabled):
    transport, calls = _transport(lambda r: httpx.Response(200, json=OK_BODY))
    assert await jev.check_key("", transport=transport) == "disabled"
    assert calls == []


async def test_check_key_never_raises(enabled, monkeypatch):
    async def boom(*a, **k):
        raise UnicodeEncodeError("latin-1", "é", 0, 1, "ordinal not in range")

    monkeypatch.setattr(jev, "ask", boom)
    assert await jev.check_key(KEY) == "unexpected:UnicodeEncodeError"
