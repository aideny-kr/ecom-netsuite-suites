"""Apply interpreted user intent to connected sources before any data tool runs.

Language understanding belongs to the configured model. Availability, verified
card state, task continuity and source exclusions are enforced here in code.
This conversational gate does not authorize connector access or financial writes.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.chat.request_routing import RequestContext, RequestRoute, previous_request_context
from app.services.chat.tool_inventory import available_data_sources


@dataclass(frozen=True)
class SourceSelection:
    question: str | None = None
    selected_sources: tuple[str, ...] = ()
    transaction_workflow: bool = False
    request_context: dict | None = None


def _chosen_card_source(history: list[dict]) -> str | None:
    """Only a resolved server card in the active context can establish a pick."""
    for message in reversed(history):
        if message.get("role") != "assistant":
            continue
        card = message.get("structured_output")
        if not isinstance(card, dict):
            continue
        if card.get("type") == "clarification" and card.get("status") == "chosen":
            options = card.get("options")
            chosen_id = card.get("chosen_id")
            if chosen_id in ("A", "B", "C") and isinstance(options, list):
                chosen = [o for o in options if isinstance(o, dict) and o.get("id") == chosen_id]
                if len(chosen) == 1 and isinstance(chosen[0].get("source"), str):
                    return chosen[0]["source"]
        if "request_context" in card:
            break
    return None


def _source_question(sources: dict[str, str], *, unavailable: bool = False) -> str:
    labels = sorted(sources.values())
    if not labels:
        return "None of the connected data sources meet your source choice. Which source would you like to use?"
    choices = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " or " + labels[-1]
    prefix = "The requested source selection is unavailable for this turn. " if unavailable else ""
    return prefix + f"Which data source should I use for this question: {choices}?"


def resolve_source_selection(
    *,
    task: str,
    tool_definitions: list[dict],
    conversation_history: list[dict] | None = None,
    context_need: str = "full",
    route: RequestRoute | None = None,
) -> SourceSelection:
    """Validate the semantic decision against server-owned task/connector state.

    A caller without a classified route may ask a source question but cannot
    infer a choice by matching words in user or assistant text.
    """
    history = conversation_history or []
    previous = previous_request_context(history)
    route = route or RequestRoute(kind="analytics", continuation=True)
    if context_need.lower() in {"docs", "workspace"}:
        route = RequestRoute(kind="conversation", continuation=True)
    if route.kind != "analytics":
        if route.kind == "conversation" and route.continuation and previous is None:
            return SourceSelection()
        state = previous if route.kind == "conversation" and route.continuation else None
        state = state or RequestContext(kind=route.kind)
        tool_names = {t.get("name", "").replace(".", "_") for t in tool_definitions}
        return SourceSelection(
            transaction_workflow=route.kind == "transaction"
            and bool(
                tool_names & {"transaction_ops_status", "transaction_ops_groups", "transaction_ops_accounting_evidence"}
            ),
            request_context=state.model_dump(),
        )

    available = available_data_sources(tool_definitions)
    intent = route.source_intent
    active = previous if route.continuation and previous and previous.kind == "analytics" else None
    excluded = set(active.excluded_sources if active else [])
    # A new explicit positive choice can reverse an earlier refusal. A refusal
    # on this turn always wins; contradictory current intent is schema-invalid.
    excluded.difference_update(intent.sources)
    excluded.update(intent.excluded)
    eligible = {source: label for source, label in available.items() if source not in excluded}
    selected = set()
    if intent.action == "select":
        selected = set(intent.sources)
    elif intent.action == "unchanged" and route.continuation:
        card_source = _chosen_card_source(history)
        selected = {card_source} if card_source else set(active.sources if active else [])
    selected.difference_update(excluded)
    unavailable = bool(selected - set(available))
    question = None
    if unavailable:
        selected.clear()  # Never silently reduce a requested cross-source comparison.
        question = _source_question(eligible, unavailable=True)
    elif not selected:
        if len(available) == 1 and eligible and intent.action == "unchanged":
            selected = set(eligible)
        else:
            question = _source_question(eligible)
    state = RequestContext(
        kind="analytics", sources=sorted(selected), excluded_sources=sorted(excluded), pending_source=bool(question)
    )
    return SourceSelection(question=question, selected_sources=tuple(state.sources), request_context=state.model_dump())


def source_selection_question(**kwargs) -> str | None:
    return resolve_source_selection(**kwargs).question
