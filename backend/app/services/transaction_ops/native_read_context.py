"""Safe provider timing context; never SQL, URLs, credentials or record IDs."""

OPERATIONS = frozenset({"suiteql", "refund_requests", "refund_graph", "record", "record_metadata"})


def operation(method, path, body):
    if method == "POST" and path == "/query/v1/suiteql":
        sql = body.get("q", "") if isinstance(body, dict) else ""
        if isinstance(sql, str):
            if "FROM customrecord_fw_refund_requests " in sql:
                return "refund_requests"
            if "FROM NextTransactionLink " in sql:
                return "refund_graph"
        return "suiteql"
    return "record_metadata" if path.startswith("/record/v1/metadata-catalog/") else "record"


def safe_context(value):
    if not isinstance(value, dict):
        return None
    name = value.get("operation")
    if not isinstance(name, str) or name not in OPERATIONS:
        return None
    elapsed, timeout = value.get("elapsed_ms"), value.get("idle_timeout_seconds")
    if (
        type(elapsed) is not int
        or not 0 <= elapsed <= 300_000
        or type(timeout) is not int
        or timeout not in {25, 60, 120}
    ):
        return None
    return {"operation": name, "elapsed_ms": elapsed, "idle_timeout_seconds": timeout}
