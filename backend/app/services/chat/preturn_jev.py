"""One Jev request for the judgments made before a chat turn starts.

Today the turn waits on ``classify_request`` — a full chat-model call — and then
runs two brittle heuristics: keyword-regex importance tiers and substring skill
matching. Jev (services/typesafe/client.py) answers all of it in one request of
about a tenth of a second, the questions evaluated in parallel over one state.

What Jev is allowed to decide is deliberately narrow. The router's source rules
turn on negation, aliases and corrections ("not only Metabase but also
NetSuite"), and Jev's vendor documents it as a literal reader. So ``to_route``
replaces the LLM router only when the request is confidently non-analytics, or
is analytics with no talk of data sources at all. Everything else — and every
Jev failure — falls through to ``classify_request`` unchanged. Routing remains
conversational context only; it grants no connector or financial authority.

The importance and skill answers are recorded for comparison and drive nothing.

JEV_PRETURN_MODE: off · shadow (both run concurrently, the LLM decides, the
comparison is recorded) · live (Jev first; LLM router when ``to_route`` is None).
"""

from __future__ import annotations

import asyncio
import time

from app.core.config import settings
from app.services.chat import request_routing
from app.services.chat.llm_adapter import TokenUsage
from app.services.chat.request_routing import RequestRoute, RoutingResult, _history_excerpt, previous_request_context
from app.services.chat.skills import get_all_skills_metadata, match_skill
from app.services.importance_classifier import classify_importance
from app.services.typesafe.client import try_ask

_DECISIVE_NO, _DECISIVE_YES = 0.2, 0.8
_NO_SOURCE_TALK = 0.1

_KINDS = {
    "analytics": (
        "Retrieving or analysing data: metrics, counts, totals, trends, reports, lists, comparisons, or a "
        "follow-up that changes or breaks down such an analysis."
    ),
    "transaction": (
        "Investigating, reconciling or fixing one existing transaction case or issue group, including questions "
        "about its evidence or retries, and continuations such as 'fix it' or 'show me the evidence'."
    ),
    "operations": (
        "Another action or workflow: integrations, flow health or retries, connection setup, jobs, edits, "
        "deployment, or record changes."
    ),
    "conversation": (
        "An explanation, documentation question, greeting or acknowledgment that asks for no new data and no action."
    ),
}
_IMPORTANCE = [
    "Casual: a quick lookup or curiosity with no decision riding on it.",
    "Operational: a filtered list or figure used for a day-to-day decision.",
    "Reporting: monthly or quarterly numbers, dashboards, trends, KPIs or exports.",
    "Audit critical: financial statements, audit, compliance, board, investor or year-end figures.",
]


def build_request(task: str, history: list[dict], available_sources: dict[str, str] | None) -> tuple[dict, dict]:
    previous = previous_request_context(history)
    state = {
        "current_request": task,
        "recent_conversation": _history_excerpt(history)[-6:],
        "active_task_kind": previous.kind if previous else None,
        "available_sources": available_sources or {},
    }
    skills = {s["slug"]: s["description"] for s in get_all_skills_metadata()}
    questions = {
        "kind": {
            "type": "choice",
            "instructions": "What kind of request is `current_request`? Judge the request itself, not the history.",
            "criteria": _KINDS,
        },
        "continuation": {
            "type": "noul",
            "instructions": "Does `current_request` continue the task already under way in `recent_conversation`?",
            "criteria": {
                "true": "It follows up on, refines, or acknowledges the active task.",
                "false": "It starts a new topic or an independent analysis, or there is no active task.",
            },
        },
        "source_talk": {
            "type": "noul",
            "instructions": (
                "In `current_request`, does the user choose, refuse, compare, or ask about which data source or "
                "system to use?"
            ),
            "criteria": {
                "true": "The user names, prefers, rules out, or asks about a data source such as those in "
                "`available_sources`.",
                "false": "The user says nothing about where the data should come from.",
            },
        },
        "importance": {
            "type": "score",
            "instructions": "How much rides on the accuracy of the answer to `current_request`?",
            "criteria": _IMPORTANCE,
        },
        "skill": {
            "type": "choice",
            "instructions": "Which one skill, if any, is `current_request` asking for?",
            "criteria": {**skills, "none": "No listed skill matches the request."},
        },
        "needs_skill": {
            "type": "noul",
            "instructions": "Does `current_request` call for one of the listed specialised skills at all?",
        },
    }
    return state, questions


def to_route(answers: dict, *, floor: float, has_persisted_context: bool = True) -> RequestRoute | None:
    """A route Jev may decide alone, or None to leave it to the LLM router."""
    kind = answers["kind"]
    continuation = answers["continuation"]["noul"]
    if (kind.get("confidence") or 0) < floor:
        return None
    if _DECISIVE_NO <= continuation <= _DECISIVE_YES:
        return None
    if kind["choice"] == "analytics":
        if answers["source_talk"]["noul"] > _NO_SOURCE_TALK:
            return None
        # "unchanged" is only exact when the prior selection is PERSISTED. With no routing
        # context yet, a source the user named earlier lives only in prose, and only the LLM
        # router reads it (legacy_user_requests). A shortcut here would re-ask the user for
        # a source they already chose.
        if continuation > _DECISIVE_YES and not has_persisted_context:
            return None
    # Non-analytics routes carry no source intent by contract; analytics with no source
    # talk and a persisted (or no) prior selection is "unchanged".
    return RequestRoute(kind=kind["choice"], continuation=continuation > _DECISIVE_YES)


async def _jev(tenant_id, task, history, available_sources) -> tuple[dict | None, dict]:
    """Jev's answers, or None with the reason recorded. Cannot raise: try_ask contains
    vendor failures AND bugs in build_request, so the LLM router beside it is never harmed."""
    record = {"jev_error": None, "jev_elapsed_ms": None}
    result, reason = await try_ask(tenant_id, build=lambda: build_request(task, history, available_sources))
    if result is None:
        record["jev_error"] = reason
        return None, record
    record.update(jev_elapsed_ms=result.elapsed_ms, jev_model=result.model, jev_input_tokens=result.input_tokens)
    return result.answers, record


async def _llm(**kwargs) -> tuple[RoutingResult, int]:
    start = time.monotonic()
    # Resolved through the module at call time: request_routing.classify_request is the
    # patch point existing suites rely on, and an import-time binding made it order-dependent.
    result = await request_routing.classify_request(**kwargs)
    return result, int((time.monotonic() - start) * 1000)


def _compare(record, answers, jev_route, task: str, llm: RoutingResult | None, llm_ms: int | None) -> dict:
    """``jev_route`` is the route the caller already derived — computed once, so the record
    describes the decision that was actually taken."""
    matched = match_skill(task)
    record.update(
        would_short_circuit=jev_route is not None,
        jev_kind=answers["kind"]["choice"] if answers else None,
        jev_kind_confidence=answers["kind"].get("confidence") if answers else None,
        jev_continuation=answers["continuation"]["noul"] if answers else None,
        jev_source_talk=answers["source_talk"]["noul"] if answers else None,
        jev_importance=round(answers["importance"]["score"]) + 1 if answers else None,
        regex_importance=classify_importance(task).value,
        jev_skill=answers["skill"]["choice"] if answers else None,
        jev_needs_skill=answers["needs_skill"]["noul"] if answers else None,
        substring_skill=matched["slug"] if matched else None,
        llm_kind=llm.route.kind if llm else None,
        llm_continuation=llm.route.continuation if llm else None,
        llm_source_action=llm.route.source_intent.action if llm else None,
        llm_elapsed_ms=llm_ms,
    )
    record["kind_agree"] = record["jev_kind"] == record["llm_kind"] if answers and llm else None
    record["route_agree"] = jev_route == llm.route if jev_route is not None and llm else None
    return record


def _jev_side(record: dict, answers, task: str, llm, llm_ms, history=()) -> tuple[RequestRoute | None, dict]:
    """Everything DERIVED from Jev's answers — the route it may decide and the comparison
    record — computed inside one guard. The client already guarantees well-typed, finite,
    in-range answers; this is the second wall, so that a bug in to_route or _compare can
    only ever lose a measurement or a shortcut, never the turn. A recorded field that
    "drives nothing" must not be able to fail anything either."""
    try:
        persisted = previous_request_context(list(history)) is not None
        floor = settings.JEV_ROUTE_MIN_CONFIDENCE
        route = to_route(answers, floor=floor, has_persisted_context=persisted) if answers else None
        return route, _compare(dict(record), answers, route, task, llm, llm_ms)
    except Exception as exc:
        failed = {**record, "jev_error": f"unexpected:{type(exc).__name__}", "would_short_circuit": False}
        return None, failed


async def route_request(
    *, tenant_id, task: str, history: list[dict], adapter, model: str, available_sources: dict[str, str] | None = None
) -> tuple[RoutingResult, dict | None]:
    """Return (routing result, comparison record or None). LLM router errors propagate as before."""
    llm_kwargs = dict(task=task, history=history, adapter=adapter, model=model, available_sources=available_sources)
    mode = settings.JEV_PRETURN_MODE
    if mode not in {"shadow", "live"}:
        return await request_routing.classify_request(**llm_kwargs), None

    if mode == "shadow":
        (answers, record), (llm, llm_ms) = await asyncio.gather(
            _jev(tenant_id, task, history, available_sources), _llm(**llm_kwargs)
        )
        _, record = _jev_side(record, answers, task, llm, llm_ms, history)
        return llm, {"mode": mode, "decided_by": "llm", **record}

    answers, record = await _jev(tenant_id, task, history, available_sources)
    route, record = _jev_side(record, answers, task, None, None, history)
    if route is not None:
        return RoutingResult(route=route, usage=TokenUsage()), {"mode": mode, "decided_by": "jev", **record}
    llm, llm_ms = await _llm(**llm_kwargs)
    _, record = _jev_side(record, answers if not record.get("jev_error") else None, task, llm, llm_ms, history)
    return llm, {"mode": mode, "decided_by": "llm", **record}
