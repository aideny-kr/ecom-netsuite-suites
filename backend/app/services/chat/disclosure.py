"""Disclosure footer for the v0 intent clarification surface.

Produces a post-tool `DisclosureBlock` describing what the agent just did:
- Which data source was read
- How the query was interpreted (relative dates, etc.)
- Which implicit filters were applied (cancelled, test, subsidiary, etc.)
- Whether the user can switch to the other data source

Designed to run synchronously after the last successful data tool call,
adding ~20-50ms to the turn. Never blocks user-visible text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class DisclosureBlock:
    """Post-tool disclosure block rendered as a footer under the assistant answer."""

    source: Literal["netsuite", "bigquery"]
    interpretation: str  # one human sentence describing how the query was parsed
    implicit_filters: list[str] = field(default_factory=list)
    can_switch_source: bool = False
    is_rerun: bool = False  # set when emitted after a source switch
    failure_mode: bool = False  # set when emitted under an error message

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "interpretation": self.interpretation,
            "implicit_filters": list(self.implicit_filters),
            "can_switch_source": self.can_switch_source,
            "is_rerun": self.is_rerun,
            "failure_mode": self.failure_mode,
        }
