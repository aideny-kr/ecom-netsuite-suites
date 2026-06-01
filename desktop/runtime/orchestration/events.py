"""Typed orchestration events whose serialized JSON matches the webapp shapes.

The reuse contract (spec section 4) is HARD: each event's `to_dict()` must equal
the webapp's `ChatStreamEvent` JSON so the desktop renderer can reuse
`frontend/src/lib/chat-stream.ts`'s `normalizeStreamEvent` and the
`data-frame-table` card UNCHANGED. The shapes mirrored here (verbatim):

    text       -> {"type": "text", "content": "<delta>"}
    data_table -> {"type": "data_table",
                   "data": {"columns": [...], "rows": [[...]],
                            "row_count": <int>, "query": "<str>", "truncated": <bool>}}

`done` is a desktop-local terminal marker (not a webapp `ChatStreamEvent`); the
desktop transport uses it to close the stream and carry the per-turn token count,
the way the webapp uses its terminal `message` event:

    done       -> {"type": "done", "tokens_used": <int>}

This module is pure (stdlib only) and free of any transport/Electron/IPC import.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class TextEvent:
    """An incremental assistant text delta."""

    content: str

    def to_dict(self) -> dict[str, Any]:
        return {"type": "text", "content": self.content}


@dataclass
class DataTableEvent:
    """A rich table payload, webapp `DataTableData` shape verbatim."""

    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    query: str = ""
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "data_table",
            "data": {
                "columns": list(self.columns),
                "rows": [list(r) for r in self.rows],
                "row_count": self.row_count,
                "query": self.query,
                "truncated": self.truncated,
            },
        }

    @classmethod
    def from_tool_result(cls, result: dict[str, Any]) -> "DataTableEvent":
        """Build from a `{"columns": [...], "rows": [[...]]}` tool result.

        `row_count` is derived from the rows (no separate count in the tool
        result); `query`/`truncated` default to the empty/false values the
        webapp normalizer falls back to — the demo tool has no SQL query and is
        never truncated.
        """
        columns = list(result.get("columns", []))
        rows = [list(r) for r in result.get("rows", [])]
        return cls(columns=columns, rows=rows, row_count=len(rows))


@dataclass
class DoneEvent:
    """Terminal marker carrying the per-turn token count."""

    tokens_used: int

    def to_dict(self) -> dict[str, Any]:
        return {"type": "done", "tokens_used": self.tokens_used}
