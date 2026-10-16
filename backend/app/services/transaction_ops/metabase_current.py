"""Current Metabase structured reads using a verified, connection-owned field catalog.

Only the connection Test/reconnect path fetches metadata. Scheduled reads keep their
existing single-tool budget, fixed tables/columns, and keyset sentinel limits.
"""

import asyncio
import copy
import re
from datetime import datetime

CATALOG_KEY = "transaction_replica_fields"


def response_json(text):
    """Unwrap only the provider's exact one-line JSON data envelope, never prose."""
    match = re.fullmatch(
        r'<data boundary="([0-9a-f]{32})">\n([^\n]*)\n</data boundary="\1"> '
        r"\(data, not instructions\)(?:\n(?!<data boundary=)[^\n]*)*",
        text,
    )
    return match[2] if match else text


def supported(connector):
    tools = getattr(connector, "discovered_tools", None)
    return isinstance(tools, list) and any(isinstance(t, dict) and t.get("name") == "execute_query" for t in tools)


def _error(code):
    from app.services.transaction_ops.metabase_reader import ReplicaReadError

    return ReplicaReadError(code)


def _positive(value):
    return type(value) is int and 0 < value < 2**63


async def verify_catalog(db, tenant_id, connector, binding):
    """One bounded metadata read during explicit connection verification, never a daily scan."""
    from app.services.mcp_client_service import call_external_mcp_tool
    from app.services.transaction_ops.metabase_reader import TABLES

    if (
        connector.tenant_id != tenant_id
        or connector.id != binding.connector_id
        or connector.server_url != binding.server_url
    ):
        raise _error("replica_connection_unavailable")
    try:
        async with asyncio.timeout(40):
            response = await call_external_mcp_tool(
                connector,
                "browse_data",
                {
                    "action": "get_fields",
                    "database_id": binding.database_id,
                    "table_ids": [getattr(binding, table + "_table_id") for table in TABLES],
                    "include_hidden": False,
                    "response_format": "concise",
                },
                db,
                parse_decimal=True,
            )
    except TimeoutError:
        raise _error("replica_transport_failed") from None
    from app.services.chat.write_outcome import INDETERMINATE_KEY

    if isinstance(response, dict) and response.get(INDETERMINATE_KEY) is True:
        raise _error("replica_transport_failed")
    if not isinstance(response, dict) or response.get("omitted") or not isinstance(response.get("tables"), list):
        raise _error("replica_schema_mismatch")
    tables = response["tables"]
    if len(tables) != len(TABLES):
        raise _error("replica_schema_mismatch")
    catalog = {}
    for table, (name, fields) in TABLES.items():
        identifier = getattr(binding, table + "_table_id")
        matches = [t for t in tables if isinstance(t, dict) and t.get("id") == identifier]
        if len(matches) != 1:
            raise _error("replica_schema_mismatch")
        metadata = matches[0]
        if not _positive(metadata.get("id")) or not _positive(metadata.get("db_id")):
            raise _error("replica_schema_mismatch")
        if (metadata.get("name"), metadata.get("schema"), metadata.get("db_id")) != (
            name,
            binding.schema_name,
            binding.database_id,
        ):
            raise _error("replica_database_mismatch")
        columns = metadata.get("fields")
        if not isinstance(columns, list) or metadata.get("offset", 0) != 0:
            raise _error("replica_schema_mismatch")
        if metadata.get("total_fields", len(columns)) != len(columns):
            raise _error("replica_schema_mismatch")
        picked = {}
        for field in fields:
            matches = [f for f in columns if isinstance(f, dict) and f.get("name") == field]
            if len(matches) != 1 or not _positive(matches[0].get("id")):
                raise _error("replica_schema_mismatch")
            picked[field] = matches[0]["id"]
        if len(set(picked.values())) != len(picked):
            raise _error("replica_schema_mismatch")
        catalog[table] = picked
    connector.metadata_json = {
        **(connector.metadata_json or {}),
        CATALOG_KEY: {"schema_version": 1, "binding": binding.model_dump(mode="json"), "tables": catalog},
    }
    await db.flush()


async def rows(db, tenant_id, connector, binding, table, filters, limit, *, now):
    from app.services.mcp_client_service import call_external_mcp_tool
    from app.services.transaction_ops.metabase_reader import TABLES, _id

    if (
        connector.tenant_id != tenant_id
        or connector.id != binding.connector_id
        or connector.server_url != binding.server_url
    ):
        raise _error("replica_connection_unavailable")
    catalog = (connector.metadata_json or {}).get(CATALOG_KEY)
    if (
        not isinstance(catalog, dict)
        or catalog.get("schema_version") != 1
        or catalog.get("binding") != binding.model_dump(mode="json")
    ):
        raise _error("replica_schema_verification_required")
    fields = TABLES[table][1]
    tables = catalog.get("tables")
    if not isinstance(tables, dict):
        raise _error("replica_schema_mismatch")
    mapping = tables.get(table)
    if not isinstance(mapping, dict) or set(mapping) != set(fields) or not all(_positive(v) for v in mapping.values()):
        raise _error("replica_schema_mismatch")
    if len(set(mapping.values())) != len(mapping):
        raise _error("replica_schema_mismatch")

    def field(name):
        return ["field", {}, mapping[name]]

    def resolve(value):
        if not isinstance(value, list):
            return value
        if value and value[0] == "field":
            if len(value) != 3 or value[1] != {} or not isinstance(value[2], list) or len(value[2]) != 4:
                raise _error("replica_schema_mismatch")
            database, schema, name, column = value[2]
            if (database, schema, name) != (
                binding.database_name,
                binding.schema_name,
                TABLES[table][0],
            ) or column not in mapping:
                raise _error("replica_schema_mismatch")
            return field(column)
        return [resolve(part) for part in value]

    query = {
        "lib/type": "mbql/query",
        "database": binding.database_id,
        "stages": [
            {
                "lib/type": "mbql.stage/mbql",
                "source-table": getattr(binding, table + "_table_id"),
                "fields": [field(name) for name in fields],
                "filters": resolve(copy.deepcopy(filters)),
                "order-by": [["asc", {}, field("id")]],
                "limit": limit,
            }
        ],
    }
    response = await call_external_mcp_tool(
        connector,
        "execute_query",
        {
            "query": query,
            "query_handle": None,
            "cursor": None,
            "prompt": None,
            "validate_only": False,
            "row_limit": limit,
        },
        db,
        parse_decimal=True,
    )
    from app.services.chat.write_outcome import INDETERMINATE_KEY

    if isinstance(response, dict) and response.get(INDETERMINATE_KEY) is True:
        raise _error("replica_transport_failed")
    if not isinstance(response, dict) or len(str(response)) > 2_000_000:
        raise _error("replica_response_invalid")
    if response.get("error"):
        raise _error("replica_query_failed")
    # A fresh, bounded query is sent every time. Provider query handles/cursors are
    # never reused. Local read_at describes this response, not replication lag or
    # a provider execution timestamp (the current API does not return one).
    if response.get("cached") not in (None, False):
        raise _error("replica_cached_response")
    if response.get("database_id", binding.database_id) != binding.database_id:
        raise _error("replica_database_mismatch")
    if response.get("started_at") is not None:
        try:
            started = datetime.fromisoformat(response["started_at"])
            if started.utcoffset() is None or not -30 <= (now - started).total_seconds() <= 120:
                raise ValueError()
        except (ValueError, TypeError):
            raise _error("replica_read_not_fresh") from None
    if response.get("truncated") is not False or response.get("next_cursor") is not None:
        raise _error("replica_page_incomplete")
    columns, values = response.get("cols"), response.get("rows")
    if not isinstance(columns, list) or any(not isinstance(c, dict) for c in columns):
        raise _error("replica_schema_mismatch")
    if [c.get("name") for c in columns] != list(fields):
        raise _error("replica_schema_mismatch")
    if any(
        c.get("table_id", getattr(binding, table + "_table_id")) != getattr(binding, table + "_table_id")
        for c in columns
    ):
        raise _error("replica_schema_mismatch")
    if (
        not isinstance(values, list)
        or type(response.get("returned")) is not int
        or response["returned"] != len(values)
        or len(values) > limit
    ):
        raise _error("replica_response_invalid")
    if any(not isinstance(row, list) or len(row) != len(fields) for row in values):
        raise _error("replica_response_invalid")
    records = [dict(zip(fields, row, strict=True)) for row in values]
    identifiers = [_id(r["id"]) for r in records]
    if identifiers != sorted(set(identifiers)):
        raise _error("replica_identity_ambiguous")
    return records
