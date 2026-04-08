"""Disclosure footer assembly — post-turn interpretation and filter extraction.

This module exposes:
- DisclosureBlock: the payload sent to the frontend as a `disclosure` SSE event.
- assemble_disclosure(): entry point called from the orchestrator after the
  final tool call has executed and before the final `message` event is yielded.
- compute_can_switch_source(): decides if the other data source is viable
  for this query class + tenant + connector health.
- classify_query_source_class(): maps a user query to a source-class label
  (orders / gl / marketing / saved_search / unknown).
- SOURCE_SWITCH_RE / PUSHBACK_RE: anchored regexes used by the chat endpoint.
- WHERE-clause parser helpers (internal).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal


@dataclass
class DisclosureBlock:
    """Post-turn footer data sent to the frontend as a `disclosure` SSE event."""

    source: Literal["netsuite", "bigquery"]
    interpretation: str
    implicit_filters: list[str] = field(default_factory=list)
    can_switch_source: bool = False
    is_rerun: bool = False
    failure_mode: bool = False

    def to_dict(self) -> dict:
        return asdict(self)
