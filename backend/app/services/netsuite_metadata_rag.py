"""Seed RAG DocChunk records from discovered NetSuite metadata.

Creates tenant-specific vector-embedded documents so the AI chat can
retrieve custom field information via similarity search, even before
inspecting the system prompt.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import DocChunk
from app.models.netsuite_metadata import NetSuiteMetadata
from app.services.chat.embeddings import embed_texts

logger = structlog.get_logger()

_SOURCE_PREFIX = "netsuite_metadata/"


# ──────────────────────────────────────────────────────────────────
# Document formatters — one per metadata category
# ──────────────────────────────────────────────────────────────────


def _format_body_fields(fields: list[dict]) -> str:
    lines = [
        "NetSuite Custom Transaction Body Fields (custbody_*)",
        "These fields are available on the `transaction` table in SuiteQL.",
        "",
    ]
    for f in fields:
        mandatory = " [REQUIRED]" if f.get("ismandatory") == "T" else ""
        desc = f" (value type: {f['fieldvaluetype']})" if f.get("fieldvaluetype") else ""
        lines.append(f"- {f.get('scriptid')}: {f.get('name')} (type: {f.get('fieldtype')}){mandatory}{desc}")
    return "\n".join(lines)


def _format_column_fields(fields: list[dict]) -> str:
    lines = [
        "NetSuite Custom Transaction Line Fields (custcol_*)",
        "These fields are available on the `transactionline` table in SuiteQL.",
        "",
    ]
    for f in fields:
        desc = f" (value type: {f['fieldvaluetype']})" if f.get("fieldvaluetype") else ""
        lines.append(f"- {f.get('scriptid')}: {f.get('name')} (type: {f.get('fieldtype')}){desc}")
    return "\n".join(lines)


def _format_entity_fields(fields: list[dict]) -> str:
    lines = [
        "NetSuite Custom Entity Fields (custentity_*)",
        "These fields are available on `customer`, `vendor`, and `employee` tables.",
        "",
    ]
    for f in fields:
        vtype = f" (value type: {f['fieldvaluetype']})" if f.get("fieldvaluetype") else ""
        lines.append(f"- {f.get('scriptid')}: {f.get('name')} (type: {f.get('fieldtype')}){vtype}")
    return "\n".join(lines)


def _format_item_fields(fields: list[dict]) -> str:
    lines = [
        "NetSuite Custom Item Fields (custitem_*)",
        "These fields are available on the `item` table in SuiteQL.",
        "",
    ]
    for f in fields:
        lines.append(f"- {f.get('scriptid')}: {f.get('name')} (type: {f.get('fieldtype')})")
    return "\n".join(lines)


def _format_custom_records(records: list[dict]) -> str:
    lines = [
        "NetSuite Custom Record Types",
        "These are custom record definitions in the account.",
        "",
    ]
    for r in records:
        desc = f" — {r['description']}" if r.get("description") else ""
        lines.append(f"- {r.get('scriptid')}: {r.get('name')}{desc}")
    return "\n".join(lines)


def _format_custom_record_fields(fields: list[dict]) -> str:
    lines = [
        "NetSuite Custom Record Fields (custrecord_*)",
        "These fields belong to custom record types. Query them via: SELECT id, <field> FROM customrecord_<scriptid>",
        "The field scriptid prefix often matches its parent record type scriptid.",
        "",
    ]
    for f in fields:
        vtype = f" (value type: {f['fieldvaluetype']})" if f.get("fieldvaluetype") else ""
        lines.append(f"- {f.get('scriptid')}: {f.get('name')} (type: {f.get('fieldtype')}){vtype}")
    return "\n".join(lines)


def _format_custom_lists(lists: list[dict]) -> str:
    lines = [
        "NetSuite Custom Lists",
        "These are custom list definitions used for dropdown/select fields.",
        "",
    ]
    for cl in lists:
        desc = f" — {cl['description']}" if cl.get("description") else ""
        lines.append(f"- {cl.get('scriptid')}: {cl.get('name')}{desc}")
    return "\n".join(lines)


def _format_custom_list_values(list_name: str, values: list[dict]) -> str:
    """Format a single custom list's values for RAG retrieval."""
    lines = [
        f"Custom List Values for: {list_name}",
        "Use these exact internal IDs when filtering records by this custom list field.",
        f"SuiteQL: WHERE field_name = <id> (or use BUILTIN.DF(field_name) = '<name>')",
        "",
    ]
    for v in values:
        status = " [INACTIVE]" if v.get("isinactive") == "T" else ""
        lines.append(f"- ID {v.get('id')}: {v.get('name')}{status}")
    return "\n".join(lines)


def _format_saved_searches(searches: list[dict]) -> str:
    lines = [
        "NetSuite Saved Searches (Public)",
        "These saved searches are available in the account. Reference them by ID or title.",
        "",
    ]
    for ss in searches:
        owner = f" (owner: {ss['owner']})" if ss.get("owner") else ""
        lines.append(f"- ID {ss.get('id')}: {ss.get('title')} (record type: {ss.get('recordtype', '?')}){owner}")
    return "\n".join(lines)


def _format_org_hierarchy(
    subsidiaries: list[dict] | None,
    departments: list[dict] | None,
    classifications: list[dict] | None,
    locations: list[dict] | None,
) -> str:
    lines = [
        "NetSuite Organisational Hierarchy",
        "Subsidiaries, departments, classes, and locations configured in this account.",
        "",
    ]

    def _active(items: list[dict] | None) -> list[dict]:
        if not items:
            return []
        return [i for i in items if i.get("isinactive") != "T"]

    if subsidiaries:
        lines.append("## Subsidiaries")
        for s in _active(subsidiaries):
            parent = f" (parent: {s['parent']})" if s.get("parent") else ""
            lines.append(f"- ID {s.get('id')}: {s.get('name')}{parent}")

    if departments:
        lines.append("\n## Departments")
        for d in _active(departments):
            parent = f" (parent: {d['parent']})" if d.get("parent") else ""
            lines.append(f"- ID {d.get('id')}: {d.get('name')}{parent}")

    if classifications:
        lines.append("\n## Classes")
        for c in _active(classifications):
            parent = f" (parent: {c['parent']})" if c.get("parent") else ""
            lines.append(f"- ID {c.get('id')}: {c.get('name')}{parent}")

    if locations:
        lines.append("\n## Locations")
        for loc in _active(locations):
            parent = f" (parent: {loc['parent']})" if loc.get("parent") else ""
            lines.append(f"- ID {loc.get('id')}: {loc.get('name')}{parent}")

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────
# Main seeding function
# ──────────────────────────────────────────────────────────────────


async def seed_metadata_docs(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    metadata: NetSuiteMetadata,
) -> int:
    """Create or replace RAG DocChunk records from discovered metadata.

    Returns the number of chunks created.
    """
    # 1. Delete previous metadata docs for this tenant
    await db.execute(
        delete(DocChunk).where(
            DocChunk.tenant_id == tenant_id,
            DocChunk.source_path.like(f"{_SOURCE_PREFIX}%"),
        )
    )

    # 2. Build chunks from non-empty metadata categories
    raw_chunks: list[tuple[str, str, str]] = []  # (source_path, title, content)

    if metadata.transaction_body_fields and isinstance(metadata.transaction_body_fields, list):
        raw_chunks.append(
            (
                f"{_SOURCE_PREFIX}transaction_body_fields",
                "NetSuite Custom Transaction Body Fields",
                _format_body_fields(metadata.transaction_body_fields),
            )
        )

    if metadata.transaction_column_fields and isinstance(metadata.transaction_column_fields, list):
        raw_chunks.append(
            (
                f"{_SOURCE_PREFIX}transaction_column_fields",
                "NetSuite Custom Transaction Line Fields",
                _format_column_fields(metadata.transaction_column_fields),
            )
        )

    if metadata.entity_custom_fields and isinstance(metadata.entity_custom_fields, list):
        raw_chunks.append(
            (
                f"{_SOURCE_PREFIX}entity_custom_fields",
                "NetSuite Custom Entity Fields",
                _format_entity_fields(metadata.entity_custom_fields),
            )
        )

    if metadata.item_custom_fields and isinstance(metadata.item_custom_fields, list):
        raw_chunks.append(
            (
                f"{_SOURCE_PREFIX}item_custom_fields",
                "NetSuite Custom Item Fields",
                _format_item_fields(metadata.item_custom_fields),
            )
        )

    if metadata.custom_record_types and isinstance(metadata.custom_record_types, list):
        raw_chunks.append(
            (
                f"{_SOURCE_PREFIX}custom_record_types",
                "NetSuite Custom Record Types",
                _format_custom_records(metadata.custom_record_types),
            )
        )

    if metadata.custom_record_fields and isinstance(metadata.custom_record_fields, list):
        raw_chunks.append(
            (
                f"{_SOURCE_PREFIX}custom_record_fields",
                "NetSuite Custom Record Fields",
                _format_custom_record_fields(metadata.custom_record_fields),
            )
        )

    if metadata.custom_lists and isinstance(metadata.custom_lists, list):
        raw_chunks.append(
            (
                f"{_SOURCE_PREFIX}custom_lists",
                "NetSuite Custom Lists",
                _format_custom_lists(metadata.custom_lists),
            )
        )

    # Per-list value RAG chunks (one per custom list with values)
    if getattr(metadata, "custom_list_values", None) and isinstance(metadata.custom_list_values, dict):
        for table_name, values in metadata.custom_list_values.items():
            if isinstance(values, list) and values:
                raw_chunks.append(
                    (
                        f"{_SOURCE_PREFIX}custom_list_values/{table_name}",
                        f"Custom List Values: {table_name}",
                        _format_custom_list_values(table_name, values),
                    )
                )

    # Saved searches
    if getattr(metadata, "saved_searches", None) and isinstance(metadata.saved_searches, list):
        raw_chunks.append(
            (
                f"{_SOURCE_PREFIX}saved_searches",
                "NetSuite Saved Searches",
                _format_saved_searches(metadata.saved_searches),
            )
        )

    # Org hierarchy as a single combined chunk
    if any([metadata.subsidiaries, metadata.departments, metadata.classifications, metadata.locations]):
        raw_chunks.append(
            (
                f"{_SOURCE_PREFIX}org_hierarchy",
                "NetSuite Organisational Hierarchy",
                _format_org_hierarchy(
                    metadata.subsidiaries,
                    metadata.departments,
                    metadata.classifications,
                    metadata.locations,
                ),
            )
        )

    if not raw_chunks:
        return 0

    # 3. Embed all chunks
    texts = [c[2] for c in raw_chunks]
    embeddings = await embed_texts(texts)

    # 4. Create DocChunk records
    for i, (source_path, title, content) in enumerate(raw_chunks):
        chunk = DocChunk(
            tenant_id=tenant_id,
            source_path=source_path,
            title=title,
            chunk_index=0,
            content=content,
            token_count=len(content.split()),
            embedding=embeddings[i] if embeddings else None,
            metadata_={"type": "netsuite_metadata", "version": metadata.version},
        )
        db.add(chunk)

    await db.flush()

    logger.info(
        "metadata.rag_docs_seeded",
        tenant_id=str(tenant_id),
        chunk_count=len(raw_chunks),
        embedded=embeddings is not None,
    )
    return len(raw_chunks)
