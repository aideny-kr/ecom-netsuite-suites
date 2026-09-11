"""Resolve user source choices before the agent can execute data tools."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.chat.request_routing import RequestContext, RequestRoute, previous_request_context
from app.services.chat.tool_inventory import available_data_sources

_SOURCE_NAMES = {
    "metabase": re.compile(r"\bmetabase\b", re.I),
    "netsuite": re.compile(r"\bnet\s*suite\b", re.I),
    "bigquery": re.compile(r"\bbig\s*query\b", re.I),
    "shopify": re.compile(r"\bshopify\b", re.I),
    "stripe": re.compile(r"\bstripe\b", re.I),
}


@dataclass(frozen=True)
class SourceSelection:
    question: str | None = None
    selected_sources: tuple[str, ...] = ()
    transaction_workflow: bool = False
    request_context: dict | None = None


def _resolve_analytics_choice(
    *,
    task: str,
    tool_definitions: list[dict],
    conversation_history: list[dict] | None = None,
    context_need: str = "full",
) -> SourceSelection:
    """Resolve user text and verified card choices separately from LLM history.

    Assistant choices and automatic source pins do not count. This is a
    conversational gate, not authorization to access a connector: the ordinary
    tenant, policy, and mutation checks still govern subsequent execution.
    """
    sources = available_data_sources(tool_definitions)
    if len(sources) < 2:
        return SourceSelection()
    candidates: list[tuple[str | None, str]] = [(None, task)]
    for message in reversed(conversation_history or []):
        # Only a server-persisted, resolved card establishes a UI choice.
        # Assistant prose and merely offered/default options never do.
        if message.get("role") == "assistant":
            card = message.get("structured_output")
            if isinstance(card, dict) and card.get("type") == "clarification" and card.get("status") == "chosen":
                chosen_id = card.get("chosen_id")
                options = card.get("options")
                if chosen_id in ("A", "B", "C") and isinstance(options, list):
                    chosen = [o for o in options if isinstance(o, dict) and o.get("id") == chosen_id]
                    if len(chosen) == 1 and isinstance(chosen[0].get("source"), str):
                        candidates.append((chosen[0]["source"], ""))
        if message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            candidates.append((None, content))
        elif isinstance(content, list):
            candidates.extend(
                (None, block["text"])
                for block in content
                if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
            )
    for chosen_source, message in candidates:
        if chosen_source is not None:
            if chosen_source in sources:
                return SourceSelection(selected_sources=(chosen_source,))
            break
        mentions = {source: list(pattern.finditer(message)) for source, pattern in _SOURCE_NAMES.items()}
        mentions = {source: matches for source, matches in mentions.items() if matches}
        if not mentions:
            continue
        selected = {
            source
            for source, matches in mentions.items()
            if source in sources
            and any(
                not re.search(
                    r"\b(?:not|don't|do not|avoid|other than|except)\s+(?:(?:use|want|from|using)\s+)*$",
                    message[max(0, match.start() - 45) : match.start()],
                    re.I,
                )
                for match in matches
            )
        }
        asking_which = re.search(r"\bwhich\s+(?:data\s+)?(?:source|database)\b", message, re.I)
        comparing = re.search(r"\b(?:compare|versus|vs\.?|both|across|reconcile)\b", message, re.I)
        if not asking_which and (len(selected) == 1 or (len(selected) > 1 and comparing)):
            return SourceSelection(selected_sources=tuple(sorted(selected)))
        # The newest mention supersedes earlier choices, including a refusal
        # or a request for an unavailable source. Do not revive an old choice.
        break
    labels = sorted(sources.values())
    choices = ", ".join(labels[:-1]) + " or " + labels[-1]
    return SourceSelection(question=f"Which data source should I use for this question: {choices}?")


def resolve_source_selection(
    *,
    task: str,
    tool_definitions: list[dict],
    conversation_history: list[dict] | None = None,
    context_need: str = "full",
    route: RequestRoute | None = None,
) -> SourceSelection:
    """Apply source choice only to analytics; preserve a task-scoped decision.

    The caller classifies purpose/continuity before this deterministic gate.
    Context hints describe prompt size, not whether an operation needs analytics.
    """
    history = conversation_history or []
    previous = previous_request_context(history)
    route = route or RequestRoute(kind="analytics", continuation=True)
    if context_need.lower() in {"docs", "workspace"}:
        route = RequestRoute(kind="conversation", continuation=True)
    if route.kind != "analytics":
        if route.kind == "conversation" and route.continuation and previous is None:
            # An acknowledgment in a legacy chat must not erase its explicit
            # user choice before that analysis has acquired persisted context.
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

    choice_history = []
    if route.continuation:
        if previous is None:
            # Existing sessions from before task-context persistence.
            has_marker = any(
                m.get("role") == "assistant"
                and isinstance(m.get("structured_output"), dict)
                and "request_context" in m["structured_output"]
                for m in history
            )
            choice_history = [] if has_marker else history
        else:
            for index in range(len(history) - 1, -1, -1):
                output = history[index].get("structured_output")
                if (
                    history[index].get("role") == "assistant"
                    and isinstance(output, dict)
                    and "request_context" in output
                ):
                    choice_history = history[index:]
                    break
            if previous.kind == "analytics" and previous.sources:
                choice_history = [
                    {"role": "user", "content": "Compare " + " and ".join(previous.sources)}
                ] + choice_history
    choice = _resolve_analytics_choice(
        task=task,
        tool_definitions=tool_definitions,
        conversation_history=choice_history,
    )
    state = RequestContext(
        kind="analytics", sources=list(choice.selected_sources), pending_source=bool(choice.question)
    )
    return SourceSelection(
        question=choice.question,
        selected_sources=choice.selected_sources,
        request_context=state.model_dump(),
    )


def source_selection_question(**kwargs) -> str | None:
    return resolve_source_selection(**kwargs).question
