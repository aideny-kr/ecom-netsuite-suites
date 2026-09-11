"""Positive read catalog for authenticated, native Metabase Cloud MCP tools.

Generic custom servers retain their approval requirement. Neither a tool's
name nor a server-provided readOnlyHint establishes authority on its own.
"""

from urllib.parse import urlsplit

_READ_TOOLS = frozenset({"search", "read_resource", "construct_query", "query", "execute_query", "execute_question"})


def is_read_only_metabase_tool(connector, raw_tool_name: str) -> bool:
    if raw_tool_name not in _READ_TOOLS or getattr(connector, "auth_type", None) != "oauth2":
        return False
    if getattr(connector, "provider", None) != "custom":
        return False
    metadata = getattr(connector, "metadata_json", None)
    if not isinstance(metadata, dict) or metadata.get("oauth_provider") != "metabase":
        return False
    server_url = getattr(connector, "server_url", None)
    if not isinstance(server_url, str):
        return False
    try:
        url = urlsplit(server_url)
        return (
            url.scheme == "https"
            and bool(url.hostname and url.hostname.endswith(".metabaseapp.com"))
            and url.port in (None, 443)
            and not url.username
            and not url.password
            and not url.query
            and not url.fragment
            and url.path == "/api/metabase-mcp"
        )
    except ValueError:
        return False


def requires_custom_tool_confirmation(connector, raw_tool_name: str) -> bool:
    return getattr(connector, "provider", None) in {"custom", "shopify_mcp", "stripe_mcp"} and not (
        is_read_only_metabase_tool(connector, raw_tool_name)
    )


def metabase_query_input_error(connector, raw_tool_name: str, params: dict) -> str | None:
    """Native read-tool names cannot authorize a SQL/native query passthrough."""
    if not is_read_only_metabase_tool(connector, raw_tool_name):
        return None
    if raw_tool_name not in {"query", "construct_query", "execute_query", "execute_question"}:
        return None
    if "query" not in params:
        # Opaque handles and saved-question identifiers follow the native read
        # protocol. They do not accept a SQL/native query body from the caller.
        return None
    query = params["query"]
    if query is None and params.get("query_handle") and raw_tool_name in {"query", "execute_query"}:
        return None
    valid = (
        isinstance(query, dict)
        and query.get("lib/type") == "mbql/query"
        and isinstance(query.get("stages"), list)
        and bool(query["stages"])
    )

    def contains_native(value):
        if isinstance(value, dict):
            return (
                any(key in {"native", "native-query"} for key in value)
                or value.get("type") == "native"
                or (str(value.get("lib/type", "")).endswith("/native"))
                or any(contains_native(child) for child in value.values())
            )
        return isinstance(value, list) and any(contains_native(child) for child in value)

    if not valid or contains_native(query):
        return (
            "Metabase analytics accepts structured MBQL query objects only. "
            "SQL strings and native query stages were not executed. Use the query tool with verified MBQL."
        )
    return None
