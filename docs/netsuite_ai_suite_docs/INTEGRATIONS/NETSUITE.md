# NetSuite Integration
_Last updated: 2026-02-15_

## Approaches we support
### A) SuiteTalk REST Web Services (default)
- Use SuiteQL REST endpoint for reporting and read-only enrichment.
- Prefer OAuth 2.0 where customers enable it; support alternatives as needed.

### B) RESTlet (optional optimization / fallback)
- Custom RESTlet to run SuiteQL internally or to perform specialized pulls/pushes when required.

## SuiteQL Tool Contract
`netsuite.suiteql(q, options)`
- Inject tenant NetSuite context (subsidiary/currency conventions)
- Default LIMIT 100; cap max rows
- Allowlist tables/record types
- Log who/what/when and execution metadata

## Auth
- OAuth 2.0 supported for REST web services and RESTlets.
- Where required by customer constraints, support other NetSuite-approved methods.

## RAG sources (Admin Copilot)
- Custom fields + custom records metadata
- Script metadata and summaries (SuiteScripts)
- Saved searches and relevant customizations
