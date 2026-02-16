async def execute(params: dict) -> dict:
    """Stub: Execute a SuiteQL query against NetSuite."""
    query = params.get("query", "")
    limit = params.get("limit", 100)

    return {
        "columns": [],
        "rows": [],
        "row_count": 0,
        "truncated": False,
        "query": query,
        "limit": limit,
        "message": "Stub: SuiteQL execution not yet implemented",
    }
