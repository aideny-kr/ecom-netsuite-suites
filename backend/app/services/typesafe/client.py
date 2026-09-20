"""The one door through which tenant data reaches TypeSafe's Jev model.

Jev is a decision model: a ``state`` plus typed questions (choice / noul /
score) in, typed answers with probabilities out. It generates nothing.

Every caller must use ``ask``. The guards live here rather than at call sites
so a new caller cannot add a hole: no key, or a tenant outside
``JEV_TENANT_ALLOWLIST``, raises before any network I/O. There are no retries —
callers sit on latency paths and already own a slower fallback, so a failure is
reported once, with a reason, and the caller falls back.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

import httpx

from app.core.config import settings

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
_ANSWER_FIELD = {"choice": "choice", "noul": "noul", "score": "score"}


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


def _valid(questions: dict[str, dict], answers: object) -> bool:
    if not isinstance(answers, dict):
        return False
    for qid, question in questions.items():
        answer = answers.get(qid)
        kind = question.get("type")
        if not isinstance(answer, dict) or answer.get("type") != kind or _ANSWER_FIELD.get(kind) not in answer:
            return False
        if kind == "choice" and answer["choice"] not in question["criteria"]:
            return False
    return True


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
        async with httpx.AsyncClient(timeout=settings.JEV_TIMEOUT_SECONDS, transport=transport) as http:
            response = await http.post(
                ENDPOINT,
                headers={"Authorization": f"Bearer {settings.TYPESAFE_API_KEY}"},
                json={"state": state, "model": settings.JEV_MODEL, "questions": questions},
            )
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
    usage = body.get("usage") or {}
    return JevResult(
        answers=body["answers"],
        model=str(body.get("model", "")),
        input_tokens=int(usage.get("input_tokens") or 0),
        elapsed_ms=int((time.monotonic() - start) * 1000),
    )
