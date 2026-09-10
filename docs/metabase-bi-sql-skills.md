# Metabase BI, SQL, and source selection

Deployment update: see the [staging release record](metabase-bi-staging-release.md).
The release-scope section below records the initial pre-deployment validation.

With multiple query sources connected, the agent asks which source the user wants
before calling data tools. An explicit source in the request or an earlier user
message carries into follow-up questions. Assistant guesses and automatic task
augmentation do not establish the user's choice. Documentation/workspace turns
and single-source inventories bypass this gate.

The agent automatically receives `metabase_bi` and `metabase_sql` when Metabase
tools survive final inventory filtering. `/metabase-bi` and `/metabase-sql` also
work explicitly. No skill seeding or database migration is required.

The skills cover schema discovery, Solidus metric definitions, SQL dialects,
MBQL 5 joins, distinct-order counts, financial aggregation, pagination, and
control totals. Solidus names are discovery hints, not assumed tenant metadata.
Batch counts do not acquire arbitrary date or completed-sales filters.

## Wiring and execution boundaries

- `source_selection.py` gates both unified-agent entry points before model/data
  execution. Choices come from the current inventory. Existing Plan Mode cards
  and source filters now recognize Metabase connections stored as `custom`.
- `metabase_context.py` identifies explicit providers, Metabase OAuth metadata,
  or the native `/api/metabase-mcp` endpoint. External tool construction stamps a
  local source tag and connection label; remote descriptions cannot activate the
  skills on unrelated connectors.
- `_assemble_system_prompt` injects skills in the shared final assembly path.
  Updating only the orchestrator's local prompt would miss UnifiedAgent's own
  prompt construction. Explicit slash skills are deduplicated.
- Native Cloud builder tools receive MBQL input hints without changing schemas.
  Direct structured-object execution avoids dependence on construction handles.
- The streaming loop lets batches containing Metabase tools finish validation
  queries without its generic early-exit rule or stop nudge. Other sources retain
  the existing behavior; normal step/retry limits still apply.
- `metabase_tool_policy.py` permits six native read tools without write cards:
  `search`, `read_resource`, `construct_query`, `query`, `execute_query`, and
  `execute_question`. The exemption requires OAuth2, Metabase OAuth metadata, and
  an HTTPS `*.metabaseapp.com/api/metabase-mcp` endpoint. Both the agent's mutation
  classifier and the dispatcher check it against the tenant's actual connector.
- Generic custom/Shopify/Stripe calls retain exact-call confirmation. SQL, unknown
  tools, and writes are outside the exemption. Arbitrary hosts, labels, and
  remote `readOnlyHint` claims cannot obtain it. Generic confirmation signs the
  entire input rather than coercing it into a NetSuite record. Existing NetSuite
  financial-write guards remain in place.

Recognition for guidance is broader than the read exemption: self-hosted
connectors can receive skills while retaining their approval boundary. These
changes do not add SQL execution tools, OAuth scopes, or database write access.

## Live backend E2E: Sakura batch 395

Tested September 9, 2026 PDT (September 10 UTC) with the tenant's configured
Anthropic `claude-sonnet-5` model and authenticated Metabase connection. The exact
user question first produced:

> Which data source should I use for this question: BigQuery, Metabase or NetSuite?

That turn made **zero model calls and zero data-tool calls**. After the user chose
Metabase and Solidus, the agent discovered the schema and executed a distinct-
order aggregation grouped by order state.

| Order state | Distinct orders |
| --- | ---: |
| complete | 41 |
| canceled | 24 |
| **Total** | **65** |

Source: **Solidus (Reporting Copy)**, database ID 2, PostgreSQL. Qualifying line
items have `batch_id = 395` and a variant SKU matching **any** of the ten provided
SKUs. Both conditions apply to the same line item. No date, payment, shipment,
or order-state exclusion was added.

A separately authored control counted distinct `spree_line_items.order_id` for
the total and again by joined `spree_orders.state`, with identical batch/SKU
filters. It independently returned 65 total, 41 complete, and 24 canceled.
Matching line counts happened to equal order counts in this snapshot; guidance
explicitly prohibits relying on that equality when orders have multiple lines.

The final model run used seven Metabase calls, with no other-source dispatch or
confirmation interruption. It recovered from an extra MBQL query wrapper and a
missing construction handle, then executed the structured query directly. The
result was complete with no continuation token. This demonstrates successful
recovery, not error-free query generation on every attempt.

The aggregate-only [E2E evidence](metabase-bi-batch395-evidence.json) records the
actual executed MBQL, SKU filters, timestamps, model response, and independent
control queries/results. It contains no credentials or customer rows. Counts
refer to that reporting-copy snapshot and may change on later refreshes.

## Checks and release scope

- **671 backend tests passed**, covering skills/prompts, source selection,
  multi-query validation, dispatch, tenant/connector isolation, Plan Mode source
  filters, NetSuite environments, financial writes, and exact-input signing and
  tampering. Two AsyncMock warnings remain in existing clarification test fixtures.
- **55 frontend tests passed** for clarification and write-confirmation cards.
  TypeScript `tsc --noEmit`, Ruff lint/format, and whitespace checks passed.

Work is isolated on `feat/metabase-bi-sql`, based on `main` at `5d340bdb`. The live
E2E used a temporary backend process with these changes over staging's
`d0d2aed7` source, preserving its native Metabase OAuth integration and unrelated
HTTP/dispatch guards. The custom-tool confirmation compatibility changes match
that staging boundary, with the verified read catalog exempted.

The native OAuth integration already connected on staging is not yet in this
main snapshot and remains a release dependency. Integrate with that connector
implementation before release; deploying this older main snapshot alone would
omit its native Metabase token-refresh support.

No merge, deployment, service restart, or browser/HTTP chat-session E2E was
performed. Validation covers the real backend agent/model/MCP path and automated
frontend component tests.

## Reference material


Tool availability and native-query limitations were checked against the
[Metabase MCP documentation](https://www.metabase.com/docs/latest/ai/mcp),
[MCP implementation notes](https://github.com/metabase/metabase/blob/master/src/metabase/mcp/README.md),
and [Agent API reference](https://github.com/metabase/metabase/blob/master/src/metabase/agent_api/reference.md).
Dialect guidance follows the [Metabase SQL editor documentation](https://www.metabase.com/docs/latest/questions/native-editor/writing-sql).
Solidus discovery hints were checked against its
[order model](https://github.com/solidusio/solidus/blob/main/core/app/models/spree/order.rb),
[promotions documentation](https://guides.solidus.io/advanced-solidus/promotions-system/),
and [payments/refunds documentation](https://guides.solidus.io/next/advanced-solidus/payments-and-refunds/).
The connected server's tool schemas and tenant metadata remain authoritative.

MBQL syntax also follows the [native construction reference](https://github.com/metabase/metabase/blob/master/resources/metabot/prompts/tools/construct_notebook_query.md).
