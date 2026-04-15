from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.chat.knowledge_profiles.loader import KnowledgeProfile


DISAMBIGUATION_INSTRUCTION = """

## Multiple Data Sources Available
You have access to multiple data sources. Choose the best source based on the query:
- NetSuite: transactional data (orders, invoices, customers, inventory, financial reports)
- BigQuery: analytics, marketing, aggregated metrics, third-party data

If the query could be answered by multiple sources, use the most authoritative one.
If genuinely unsure, ask the user: "I can check this in [source A] or [source B]. Which would you prefer, or should I check both?"
"""


def get_active_profiles(
    profiles: list[KnowledgeProfile],
    tool_names: set[str],
) -> list[KnowledgeProfile]:
    """Return profiles whose trigger tools match the available tool set."""
    return [p for p in profiles if p.matches_tools(tool_names)]


def assemble_knowledge_context(active_profiles: list[KnowledgeProfile]) -> str:
    """Build the knowledge context string from active profiles."""
    if not active_profiles:
        return ""
    parts = []
    for profile in active_profiles:
        if profile.prompt_fragment.strip():
            parts.append(profile.prompt_fragment.rstrip())
    return "\n\n".join(parts) if parts else ""


def build_disambiguation_instruction(active_profiles: list[KnowledgeProfile]) -> str:
    """Return disambiguation instruction when multiple data sources are active."""
    if len(active_profiles) < 2:
        return ""
    return DISAMBIGUATION_INSTRUCTION


def build_source_pin_hint(source_pin: str | None) -> str:
    """Build a lightweight prompt hint for session source affinity."""
    if not source_pin:
        return ""
    source_name = {"bigquery": "BigQuery", "netsuite": "NetSuite"}.get(source_pin, source_pin)
    return (
        f"\n\n## Session Context\n"
        f"Previous queries in this session used {source_name}. "
        f"For follow-up questions, prefer {source_name} unless the query "
        f"clearly belongs to a different source."
    )


def collect_rag_partitions(active_profiles: list[KnowledgeProfile]) -> list[str]:
    """Collect all RAG partition IDs from active profiles for batched retrieval."""
    partitions: list[str] = []
    for profile in active_profiles:
        partitions.extend(profile.rag_partitions)
    return partitions
