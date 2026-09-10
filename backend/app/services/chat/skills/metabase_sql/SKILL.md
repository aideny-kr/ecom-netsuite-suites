---
Name: Metabase SQL Analysis
Description: Discover the connected database schema and dialect, build read-only analytics queries, prevent join inflation, and verify complete query results.
Triggers:
  - /metabase-sql
---

# Metabase SQL Analysis

Use only the Metabase tools present in this turn's inventory, with their actual schemas. Tool names below describe the native Metabase MCP workflow; call their full connector-prefixed names only if available. If the connector exposes different tools, use their documented equivalents. No available Metabase query tool means no execution claim.

1. **Inspect before composing.** Use search and resource-reading tools to resolve the database, engine/dialect, schema, tables or models, columns/types, primary/foreign keys, metric definitions, and permitted scope. Native Metabase commonly exposes `search` and `read_resource`. Follow returned resource URIs and pagination links; do not invent IDs, URI paths, or assume one metadata page is the whole catalog. Inspect only relevant entities and small non-sensitive samples or grouped status values. Reuse verified metadata from the same connector/database in this conversation.
2. **Choose an available execution path.** Prefer verified metrics/models and the query builder for questions it can express. With native tools, `construct_query` returns an opaque query handle for `execute_query`, `query`, or supported visualization tools; pass it unchanged on the same connector. Preserve the user's question when the constructor accepts it, inspect the returned construction, and verify the chosen fields, filters, aggregation and date grain. A constructed query is not an executed result. Run saved questions only through an available tool that supports their parameters.
   - Native SQL requires an available SQL execution tool (commonly `execute_sql`) and permission on the selected database. Read-only OAuth connections may expose only query-builder execution. If native SQL is unavailable or denied, use permitted builder/metric capabilities; if they cannot express the question, explain the limitation and provide clearly labeled **unexecuted SQL** only when the schema and dialect are verified. Never retry through another connector, change scopes, or use a write tool to bypass a denial.
   - `construct_native_query`, if present, only constructs a native handle; it does not execute SQL. Do not pass native handles to builder execution tools unless their actual schema explicitly supports that. Saving a query is not a workaround for unavailable SQL execution.
3. **Use the database's dialect.** Metabase is a query interface, not a SQL dialect. Confirm the underlying engine first. For PostgreSQL use its identifier quoting, booleans, date/time functions, numeric casts and LIMIT syntax; for another engine use that engine's documented syntax. Do not import NetSuite's `BUILTIN.DF`, `ROWNUM`, T/F booleans, or SuiteQL restrictions, or BigQuery-only functions/backticks, into PostgreSQL queries. Database SQL and Metabase query-builder expressions are separate languages. Never guess a field because it exists in NetSuite or a generic Solidus example.
4. **Build a bounded, read-only query.** Use a single SELECT or read-only WITH ... SELECT. No DML, DDL, SELECT INTO, locking clauses, data-modifying CTEs, procedures, side-effecting functions, multi-statements, or permission changes. This is guidance, not a substitute for database permissions or existing execution guards. Select only required columns. Use half-open time windows (start inclusive, end exclusive) and explicit business timezone boundaries. Filter large fact tables early; a result LIMIT does not make an unbounded aggregate cheap. Parameterize values where supported; otherwise use correct dialect-specific literal escaping. Never interpolate user text as an identifier without metadata validation.
5. **Protect the grain.** Start from one row per target entity. Pre-aggregate each one-to-many child source to the join key before joining it to orders. Do not SUM order totals after joining raw line items, payments, refunds, or shipments. COUNT(DISTINCT order_id) alone does not repair an inflated SUM; SUM(DISTINCT amount) also loses legitimate equal-value orders. For product breakdowns use line-level measures, with explicit allocation if order-level amounts are needed. Respect polymorphic adjustment types as well as IDs, and verify foreign keys instead of joining unrelated numeric IDs. Use LEFT JOIN where missing children should be retained, with child filters placed to preserve that meaning.
6. **Compute and check.** Aggregate on the server. Use decimal arithmetic and protected denominators (such as NULLIF with appropriate numeric casts) for ratios; define null versus zero deliberately. Keep currency in monetary grouping. Use deterministic ordering for top-N and pagination. Check execution status and errors before consuming rows. Validate control totals and entity counts with the same population, and verify that breakdowns reconcile. Treat row limits, truncation flags, continuation tokens, hidden filters, cache age, and partial periods as limitations. Never compute population totals from a preview page; query a server-side aggregate or exhaust the supported pagination when a full detail list is required.
7. **Recover with evidence.** A schema/dialect error calls for inspecting the relevant metadata and one focused correction. A timeout calls for narrowing the population or query shape, not repeatedly rerunning it. A permission error is a boundary. An empty result calls for checking date/status/store filters before a zero-activity conclusion. Stop retrying after two failed corrections and state the precise unresolved issue. Present the executed scope and validated result; if SQL was only drafted, label it unexecuted.

Keep joins across systems out of local-only tools: `cross_source_query` and `pivot_query_result` support only their advertised sources/dialects. Do not pass Metabase SQL or query handles to them. For an explicitly requested cross-system comparison, use a verified shared key, matching grain/currency/time scope, and supported computation; report a missing join capability instead of correlating raw tables by eye.

## Native MBQL 5 construction

`construct_query` expects structured MBQL, not SQL or a natural-language request. Its reference to `construct_notebook_query` does not mean that tool is available. Use this format with names discovered from metadata:

```json
{
  "lib/type": "mbql/query",
  "stages": [{
    "lib/type": "mbql.stage/mbql",
    "source-table": ["DATABASE", "SCHEMA", "FACTS"],
    "joins": [{
      "alias": "items",
      "strategy": "inner-join",
      "stages": [{"lib/type": "mbql.stage/mbql", "source-table": ["DATABASE", "SCHEMA", "ITEMS"]}],
      "conditions": [["=", {},
        ["field", {}, ["DATABASE", "SCHEMA", "FACTS", "item_id"]],
        ["field", {"join-alias": "items"}, ["DATABASE", "SCHEMA", "ITEMS", "id"]]
      ]]
    }],
    "filters": [["in", {}, ["field", {"join-alias": "items"}, ["DATABASE", "SCHEMA", "ITEMS", "sku"]], "VALUE_A", "VALUE_B"]],
    "aggregation": [["distinct", {}, ["field", {}, ["DATABASE", "SCHEMA", "FACTS", "order_id"]]]],
    "limit": 20
  }]
}
```

This is a syntax template, never a tenant query. Replace every illustrative name/value. Joins contain nested `stages` and plural `conditions`; do not put `source-table` or singular `condition` directly on the join. Joined fields need their alias even inside conditions. Every operator has an options object. Add grouping fields under `breakout`. Additional filters belong in the stage's `filters` list. Use `distinct` for unique counts. For an order-status breakdown, retain the `distinct` order-ID aggregation and add the order-state field to `breakout`; do not replace it with `count` after joining matching lines. Run a separate distinct-order total with identical filters to validate the breakdown. Database/table/field references use portable names, not numeric IDs or resource URIs. Prefer the `query` tool's direct object execution when available; it avoids dependence on session-scoped construction handles. If a newly constructed handle is reported missing, run the same verified object through `query` instead of repeatedly reconstructing it. Preserve the original user question in the constructor's `prompt` argument.
