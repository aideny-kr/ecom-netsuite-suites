"""Discover transaction relationships from tenant NetSuite data."""

import structlog

logger = structlog.get_logger()


async def discover_transaction_relationships(
    access_token: str,
    account_id: str,
) -> list[dict]:
    """Query NetSuite to discover which transaction types are linked via createdfrom."""
    from app.services.netsuite_client import execute_suiteql_via_rest

    query = """
    SELECT DISTINCT
        BUILTIN.DF(parent.type) as source_type,
        BUILTIN.DF(child.type) as created_type,
        COUNT(*) as link_count
    FROM transaction child
    JOIN transaction parent ON parent.id = child.createdfrom
    WHERE child.createdfrom IS NOT NULL
    GROUP BY BUILTIN.DF(parent.type), BUILTIN.DF(child.type)
    ORDER BY link_count DESC
    FETCH FIRST 50 ROWS ONLY
    """

    try:
        result = await execute_suiteql_via_rest(access_token, account_id, query)
        rows = result.get("rows", [])

        relationships = []
        for row in rows:
            if len(row) >= 3:
                relationships.append({
                    "source_type": row[0],
                    "created_type": row[1],
                    "link_count": row[2],
                })

        logger.info("relationship_discovery.complete", relationships=len(relationships))
        return relationships
    except Exception as e:
        logger.warning("relationship_discovery.failed", error=str(e))
        return []
