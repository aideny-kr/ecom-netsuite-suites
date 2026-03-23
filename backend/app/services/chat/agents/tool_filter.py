"""Per-agent tool and RAG partition filtering."""

from __future__ import annotations


def get_tools_for_agent(
    all_tools: list[dict],
    tool_ids: list[str] | None,
) -> list[dict]:
    """Filter tool definitions by agent's allowed tool_ids.

    If tool_ids is None, return all tools (no filtering).
    If tool_ids is empty list, return empty list.
    Otherwise filter to only tools whose 'name' is in tool_ids.
    """
    if tool_ids is None:
        return list(all_tools)
    allowed = set(tool_ids)
    return [t for t in all_tools if t["name"] in allowed]


def filter_knowledge_by_partition(
    chunks: list[dict],
    partition_ids: list[str] | None,
) -> list[dict]:
    """Filter knowledge chunks by partition_id.

    If partition_ids is None, return all chunks.
    Otherwise return only chunks whose partition_id is in partition_ids.
    """
    if partition_ids is None:
        return list(chunks)
    allowed = set(partition_ids)
    return [c for c in chunks if c.get("partition_id") in allowed]
