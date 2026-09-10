"""Resolve user source choices before the agent can execute data tools."""

from __future__ import annotations

import re

from app.services.chat.tool_inventory import available_data_sources

_SOURCE_NAMES = {
    "metabase": re.compile(r"\b(?:metabase|solidus)\b", re.I),
    "netsuite": re.compile(r"\bnet\s*suite\b", re.I),
    "bigquery": re.compile(r"\bbig\s*query\b", re.I),
    "shopify": re.compile(r"\bshopify\b", re.I),
    "stripe": re.compile(r"\bstripe\b", re.I),
}


def source_selection_question(
    *,
    task: str,
    tool_definitions: list[dict],
    conversation_history: list[dict] | None = None,
    context_need: str = "full",
) -> str | None:
    """Return a source question when no USER message has selected one.

    Assistant choices and automatic source pins do not count. This is a
    conversational gate, not authorization to access a connector: the ordinary
    tenant, policy, and mutation checks still govern subsequent execution.
    """
    if context_need in {"docs", "workspace"}:
        return None
    sources = available_data_sources(tool_definitions)
    if len(sources) < 2:
        return None
    user_messages = [task]
    for message in reversed(conversation_history or []):
        if message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            user_messages.append(content)
        elif isinstance(content, list):
            user_messages.extend(
                block["text"] for block in content if block.get("type") == "text" and isinstance(block.get("text"), str)
            )
    for message in user_messages:
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
            return None
        # The newest mention supersedes earlier choices, including a refusal
        # or a request for an unavailable source. Do not revive an old choice.
        break
    labels = sorted(sources.values())
    choices = ", ".join(labels[:-1]) + " or " + labels[-1]
    return f"Which data source should I use for this question: {choices}?"
