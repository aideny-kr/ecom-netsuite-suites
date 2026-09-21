"""The one door through which tenant data reaches TypeSafe's Jev model.

Jev is a decision model: a ``state`` plus typed questions (choice / noul /
score) in, typed answers with probabilities out. It generates nothing.

Every caller must come through here. The guards live in this module rather than
at call sites so a new caller cannot add a hole: no key, or a tenant outside
``JEV_TENANT_ALLOWLIST``, raises before any network I/O. There are no retries —
callers sit on latency paths and already own a slower fallback, so a failure is
reported once, with a reason, and the caller falls back.

Two promises callers rely on, both made HERE so no call site has to remember them:

* A returned answer is well-typed. ``_valid`` checks values, not just keys — a
  200 carrying ``"noul": null`` once passed validation and would have raised a
  TypeError inside chat routing, costing the user their turn.
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


def tenant_allowed(tenant_id: uuid.UUID | str) -> bool:
    allowed = {t.strip().lower() for t in settings.JEV_TENANT_ALLOWLIST.split(",") if t.strip()}
    return str(tenant_id).lower() in allowed


def _number(value, low: float | None = None, high: float | None = None) -> bool:
    if isinstance(value, bool) or not isinstance(value, Real):
        return False
    return (low is None or value >= low) and (high is None or value <= high)


def _valid_answer(question: dict, answer: object) -> bool:
    kind = question.get("type")
    if not isinstance(answer, dict) or answer.get("type") != kind:
        return False
    if kind == "noul":
        return _number(answer.get("noul"), 0.0, 1.0)
    if kind == "choice":
        return (
            answer.get("choice") in question["criteria"]
            and _number(answer.get("confidence"), 0.0, 1.0)
            and isinstance(answer.get("probabilities"), dict)
        )
    if kind == "score":
        return _number(answer.get("score")) and _number(answer.get("confidence"), 0.0, 1.0)
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


async def _post(payload: dict, transport) -> httpx.Response:
    headers = {"Authorization": f"Bearer {settings.TYPESAFE_API_KEY}"}
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
    transport: httpx.AsyncBaseTransport | None = None,
) -> JevResult:
    if not settings.TYPESAFE_API_KEY:
        raise JevUnavailableError("disabled")
    if not tenant_allowed(tenant_id):
        raise JevUnavailableError("tenant_not_allowed")

    start = time.monotonic()
    try:
        response = await _post({"state": state, "model": settings.JEV_MODEL, "questions": questions}, transport)
    except httpx.TimeoutException as exc:
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


async def try_ask(tenant_id, state=None, questions=None, *, build=None, **kwargs):
    """``ask`` that cannot raise: (result, None) or (None, reason). Cancellation still propagates.

    Pass ``build`` (a zero-argument callable returning ``(state, questions)``) rather than
    prebuilt arguments when building the request is itself non-trivial: a bug in a request
    builder is a Jev-side failure too, and must not escape into the primary path.
    """
    try:
        if build is not None:
            state, questions = build()
        return await ask(tenant_id, state, questions, **kwargs), None
    except JevUnavailableError as exc:
        return None, exc.reason
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return None, f"unexpected:{type(exc).__name__}"
