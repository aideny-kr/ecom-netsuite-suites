"""The one door through which tenant data reaches TypeSafe's Jev model.

Jev is a decision model: a ``state`` plus typed questions (choice / noul /
score) in, typed answers with probabilities out. It generates nothing.

Every caller must come through here, with the key ``typesafe.access.resolve_access``
chose for the tenant (the tenant's own key, else the platform key). The client never
reads a key from settings, so no caller can send one tenant's work under another key,
and no key refuses before any network I/O. There are no retries — callers sit on
latency paths and already own a slower fallback, so a failure is reported once, with
a reason, and the caller falls back.

Three promises callers rely on, both made HERE so no call site has to remember them:

* A returned answer is well-typed. ``_valid`` checks values, not just keys — a
  200 carrying ``"noul": null`` once passed validation and would have raised a
  TypeError inside chat routing, costing the user their turn.
* Every number in a returned answer is finite and inside its range (probabilities
  and confidence in [0, 1], a score within its question's levels).
* ``try_ask`` cannot raise. Jev always runs beside a primary path (the LLM
  router, the LLM classifier); anything going wrong on the Jev side — a vendor
  outage or a bug of ours — must degrade to "no Jev answer", never reach that
  primary path. Call sites use ``try_ask``; ``ask`` is the raising core.

``session()`` lets a batch caller (the recon worker, the evaluation script)
reuse one HTTPS connection instead of paying a TLS handshake per call. It is
scoped and closed by the caller, so nothing outlives the event loop that made it.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import math
import time
import uuid
from dataclasses import dataclass
from numbers import Real

import httpx

from app.core.config import settings

ENDPOINT = "https://api.typesafe.ai/v1/systemone"

_session_client: contextvars.ContextVar[httpx.AsyncClient | None] = contextvars.ContextVar(
    "jev_session_client", default=None
)


class JevUnavailableError(Exception):
    """Jev produced no usable answer; ``reason`` says why, for logs and fallbacks."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class JevResult:
    answers: dict[str, dict]
    model: str
    input_tokens: int
    elapsed_ms: int


def _number(value, low: float | None = None, high: float | None = None) -> bool:
    # json.loads accepts NaN / Infinity, and both are instances of Real: reject them here so
    # no caller ever has to wonder whether round() or a comparison will blow up.
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        return False
    return (low is None or value >= low) and (high is None or value <= high)


def _valid_answer(question: dict, answer: object) -> bool:
    kind = question.get("type")
    if not isinstance(answer, dict) or answer.get("type") != kind:
        return False
    if kind == "noul":
        return _number(answer.get("noul"), 0.0, 1.0)
    if kind == "choice":
        probabilities = answer.get("probabilities")
        # A coherent distribution: exactly the criteria as keys, mass summing to ~1, and
        # the chosen option the most probable. confidence=1.0 over an empty or foreign
        # distribution is not a decision anyone should threshold on.
        return (
            isinstance(probabilities, dict)
            and set(probabilities) == set(question["criteria"])
            and all(_number(p, 0.0, 1.0) for p in probabilities.values())
            and abs(sum(probabilities.values()) - 1.0) <= 0.02
            and answer.get("choice") in probabilities
            and probabilities[answer["choice"]] >= max(probabilities.values()) - 1e-9
            and _number(answer.get("confidence"), 0.0, 1.0)
        )
    if kind == "score":
        top = len(question["criteria"]) - 1  # a score is a position on the question's own levels
        probabilities = answer.get("probabilities")
        return (
            _number(answer.get("score"), 0.0, top)
            and _number(answer.get("confidence"), 0.0, 1.0)
            and (probabilities is None or isinstance(probabilities, dict))
            and all(_number(p, 0.0, 1.0) for p in (probabilities or {}).values())
        )
    return False


def _valid(questions: dict[str, dict], answers: object) -> bool:
    return isinstance(answers, dict) and all(_valid_answer(q, answers.get(qid)) for qid, q in questions.items())


@contextlib.asynccontextmanager
async def session(*, transport: httpx.AsyncBaseTransport | None = None):
    """Reuse one HTTP client for every ``ask`` in this block; closed on exit."""
    async with httpx.AsyncClient(timeout=settings.JEV_TIMEOUT_SECONDS, transport=transport) as http:
        token = _session_client.set(http)
        try:
            yield
        finally:
            _session_client.reset(token)


async def _post(payload: dict, transport, api_key: str) -> httpx.Response:
    headers = {"Authorization": f"Bearer {api_key}"}
    shared = _session_client.get()
    if shared is not None and transport is None:
        return await shared.post(ENDPOINT, headers=headers, json=payload)
    async with httpx.AsyncClient(timeout=settings.JEV_TIMEOUT_SECONDS, transport=transport) as http:
        return await http.post(ENDPOINT, headers=headers, json=payload)


async def ask(
    tenant_id: uuid.UUID | str,
    state: str | dict | list,
    questions: dict[str, dict],
    *,
    api_key: str | None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> JevResult:
    if not api_key:
        raise JevUnavailableError("disabled")

    start = time.monotonic()
    try:
        # httpx's timeout bounds each connect/read/write, not the request: a body that trickles
        # in never trips it. JEV_TIMEOUT_SECONDS is a promise about the WHOLE call, so bound that.
        async with asyncio.timeout(settings.JEV_TIMEOUT_SECONDS):
            response = await _post(
                {"state": state, "model": settings.JEV_MODEL, "questions": questions}, transport, api_key
            )
    except (httpx.TimeoutException, TimeoutError) as exc:
        raise JevUnavailableError("timeout") from exc
    except httpx.HTTPError as exc:
        raise JevUnavailableError("connection_error") from exc
    if response.status_code != 200:
        raise JevUnavailableError(f"http_{response.status_code}")

    try:
        body = response.json()
    except ValueError as exc:
        raise JevUnavailableError("invalid_response") from exc
    if not isinstance(body, dict) or not _valid(questions, body.get("answers")):
        raise JevUnavailableError("invalid_response")
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    tokens = usage.get("input_tokens")
    return JevResult(
        answers=body["answers"],
        model=str(body.get("model", "")),
        input_tokens=int(tokens) if _number(tokens) else 0,
        elapsed_ms=int((time.monotonic() - start) * 1000),
    )


async def try_ask(tenant_id, state=None, questions=None, *, build=None, api_key: str | None = None, **kwargs):
    """``ask`` that cannot raise: (result, None) or (None, reason). Cancellation still propagates.

    Pass ``build`` (a zero-argument callable returning ``(state, questions)``) rather than
    prebuilt arguments when building the request is itself non-trivial: a bug in a request
    builder is a Jev-side failure too, and must not escape into the primary path.
    """
    try:
        # Refuse before building: without a key the tenant's data should not even be assembled.
        if not api_key:
            return None, "disabled"
        if build is not None:
            state, questions = build()
        return await ask(tenant_id, state, questions, api_key=api_key, **kwargs), None
    except JevUnavailableError as exc:
        return None, exc.reason
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return None, f"unexpected:{type(exc).__name__}"


# A probe carries no tenant data: one fixed question whose only purpose is to show the key works.
_PROBE_QUESTIONS = {
    "ready": {
        "type": "choice",
        "instructions": "Connection check. Answer yes.",
        "criteria": {"yes": "The service is reachable.", "no": "It is not."},
    }
}


async def check_key(api_key: str | None, *, transport: httpx.AsyncBaseTransport | None = None) -> str | None:
    """None when ``api_key`` gets a valid answer from Jev, else the reason it did not.
    Never raises (cancellation aside): a key httpx cannot put in a header is a reason too."""
    try:
        await ask("key-check", {"check": True}, _PROBE_QUESTIONS, api_key=api_key, transport=transport)
    except JevUnavailableError as exc:
        return exc.reason
    except Exception as exc:
        return f"unexpected:{type(exc).__name__}"
    return None
