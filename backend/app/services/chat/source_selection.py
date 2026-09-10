"""Resolve user source choices before the agent can execute data tools."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.chat.tool_inventory import available_data_sources

_SOURCE_NAMES = {
    "metabase": re.compile(r"\b(?:metabase|solidus)\b", re.I),
    "netsuite": re.compile(r"\bnet\s*suite\b", re.I),
    "bigquery": re.compile(r"\bbig\s*query\b", re.I),
    "shopify": re.compile(r"\bshopify\b", re.I),
    "stripe": re.compile(r"\bstripe\b", re.I),
}


@dataclass(frozen=True)
class SourceSelection:
    question: str | None = None
    selected_sources: tuple[str, ...] = ()


def resolve_source_selection(
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
    if context_need in {"docs", "workspace"}:
        return SourceSelection()
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
                    r"\b(?:not|don't|do not|avoid)\s+(?:use\s+)?$",
                    message[max(0, match.start() - 20) : match.start()],
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


def source_selection_question(**kwargs) -> str | None:
    return resolve_source_selection(**kwargs).question
