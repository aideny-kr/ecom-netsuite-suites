"""Seed BigQuery schema into RAG partitions for the BI agent.

Creates DomainKnowledgeChunk records with partition_id="bi/schema-docs".
One chunk per table with dataset, table name, columns, and types.
Idempotent — deletes existing bi/schema-docs chunks before re-seeding.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import delete

from app.models.domain_knowledge import DomainKnowledgeChunk

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_PARTITION_ID = "bi/schema-docs"


def _build_table_chunk(dataset_id: str, table: dict) -> str:
    """Build a text chunk describing a single BigQuery table."""
    table_id = table.get("table_id", "unknown")
    columns = table.get("columns", [])

    lines = [f"Table: {dataset_id}.{table_id}"]
    if columns:
        lines.append("Columns:")
        for col in columns:
            name = col.get("name", "?")
            col_type = col.get("type", "?")
            desc = col.get("description")
            if desc:
                lines.append(f"  - {name} ({col_type}): {desc}")
            else:
                lines.append(f"  - {name} ({col_type})")
    return "\n".join(lines)


async def seed_bigquery_schema(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    schema: dict,
) -> int:
    """Seed BigQuery schema into RAG partitions.

    Args:
        db: Database session.
        tenant_id: Tenant owning the BigQuery connection.
        schema: Output from bigquery_service.discover_schema(),
                shaped as ``{"datasets": [{"dataset_id": ..., "tables": [...]}]}``.

    Returns:
        Number of chunks created.
    """
    # Delete existing chunks for this partition (idempotent)
    await db.execute(
        delete(DomainKnowledgeChunk).where(
            DomainKnowledgeChunk.partition_id == _PARTITION_ID,
            DomainKnowledgeChunk.source_type == "bigquery_schema",
        )
    )

    datasets = schema.get("datasets", [])
    count = 0

    for dataset in datasets:
        dataset_id = dataset.get("dataset_id", "unknown")
        tables = dataset.get("tables", [])

        for table in tables:
            raw_text = _build_table_chunk(dataset_id, table)
            table_id = table.get("table_id", "unknown")

            chunk = DomainKnowledgeChunk(
                source_uri=f"bi/schema-docs/{dataset_id}.{table_id}",
                chunk_index=0,
                raw_text=raw_text,
                token_count=len(raw_text) // 4,
                source_type="bigquery_schema",
                partition_id=_PARTITION_ID,
                is_deprecated=False,
            )
            db.add(chunk)
            count += 1

    if count:
        await db.flush()
        print(
            f"[BI_SEEDER] Seeded {count} BigQuery schema chunks for partition {_PARTITION_ID}",
            flush=True,
        )

    return count
