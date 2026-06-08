# Cross-Source Analytics, Drive-as-Data, Visualization & Financial Narrative — Research & Architecture Recommendation

- **Date:** 2026-06-03
- **Branch:** `research/cross-source-analytics`
- **Author:** main-thread synthesis over a 17-agent hybrid codebase+web research run (`wf_87042a2f-989`; 5 read-only code mappers + 5 adversarially-verified web angles + code-grounding audit). Subagents did fact-finding only; recommendations and roadmap are authored here.
- **Scope (as chosen):** compare both join approaches → recommend; full report + roadmap; **cross-source analytics weighted heaviest**, Drive-as-RAG a major section, visualization + financial storytelling + prebuilt prompts as supporting sections.

---

## Executive Summary

**The problem.** The chat agent struggles to *join* data across BigQuery + NetSuite (+ Drive). Research confirms why, precisely: **there is no backend join engine.** A grep across `backend/app/services` and `backend/app/mcp` for `federat|join_results|merge.sources|stitch` and for `pandas`/`DataFrame.merge` returns **zero** hits. The entire cross-source "join" is two *prose strings* — `knowledge_profiles/cross_source.yaml` step 3 ("Correlate the results **in your response** — present a unified answer, not two separate tables") and `prompt_assembler.py::DISAMBIGUATION_INSTRUCTION` ("call both tools and synthesize… identify the join key"). The model is asked to eyeball two condensed previews and write prose.

**The cruel irony.** The same mechanism that makes single-source numbers *trustworthy* is what makes joining *impossible*: `orchestrator._intercept_tool_result` strips full rows out of the LLM's view (30 rows for a standard table, 5 for a saved search, **zero** for a financial report) and tells it "Do NOT reproduce the table." So the trust boundary and the join goal are in direct tension — the LLM literally cannot see enough rows to join accurately, by design.

**The recommendation (one line).** Build a **deterministic backend compute layer** — an in-process **DuckDB** join/pivot engine fed by the result sets we already cache, fronted by a small **governed metric catalog**, with a **deterministic stat/insight service** and **deterministic chart builder** — and keep the LLM as *orchestrator + narrator only*. This doesn't fight the existing architecture; it **extends the trust boundary the code already enforces per-source to the cross-source case.** Every reusable seam already exists (`pivot_service.pivot_rows`, `result_cache` holding NS+BQ tables side-by-side, `_intercept_tool_result`'s normalized `{columns, rows}` shape, `financial_chart_builder`, the `SKILL.md` slash-command system). We are connecting parts, not green-fielding.

**Why not the alternatives (full comparison in §3).** Federated query (Trino) is over-scoped for one tenant joining a few tables. Pure-LLM orchestration craters on multi-relational joins (~10% accuracy on Spider 2.0 in the literature) and is structurally blocked by our own no-numbers interception. A heavyweight external semantic layer (Cube/dbt) is the *right long-term grounding for KPIs* but only answers what's modeled (0% on un-modeled joins) — so it's a **later, additive** layer, not the starting point.

**Headline roadmap.** Phase 0 fix broken seams (dead `drive_read_doc` tool, NULL-embedding schema chunks) → Phase 1 **DuckDB cross-source join/pivot tool** (fixes the #1 pain) → Phase 2 metric catalog + cross-source/charting prebuilt prompts → Phase 3 Drive-as-data (the already-callable `sheets.read_range` becomes a joinable table) → Phase 4 deterministic charts + grounded financial narrative → Phase 5 graduate (external semantic layer over MCP, chart regression CI). Each phase is independently shippable and runs through the existing vs-MCP benchmark gate.

---

## 1. Current State — What This System Already Has

The chat pipeline is a **single `UnifiedAgent`** (`backend/app/services/chat/agents/unified_agent.py`) driven by a ~3,378-line orchestrator (`backend/app/services/chat/orchestrator.py`). There is no multi-agent router — `DataAnalysisAgent`/`SuiteQLAgent` exist in `agents/__init__.py` but are never instantiated (dead code). Domain behavior is layered via tool-triggered YAML **knowledge profiles** (`knowledge_profiles/*.yaml` + `loader.py`) plus `prompt_assembler.py`.

### 1.1 Cross-source orchestration & joining
- **No backend join engine** (audit-confirmed by grep — zero hits for join/federation/merge primitives).
- Cross-source is **prompt-level only**: `cross_source.yaml` (triggers when both `bigquery_sql` + `netsuite_suiteql` are available) step 3 = "correlate in your response"; `DISAMBIGUATION_INSTRUCTION` (lines 9–23) = "call both tools and synthesize." Both are prose, not code. **Drive is absent from join guidance.**
- Each source = an independent tool call. `_intercept_tool_result` (l.718) emits the **full** table to the frontend as a separate `data_table`/`financial_report` SSE event and returns only a **condensed preview** to the LLM.
- `_compute_source_pin_update` (l.684) returns `None` (clears the session source pin) on a mixed BQ+NetSuite turn — the system treats cross-source as a state to **forget**, not a joined state to anchor on.
- The only deterministic server-side tabular compute is **single-source**: `pivot_tool.py` re-executes ONE query (SuiteQL *or* BigQuery via `_detect_dialect`) and pivots through `pivot_service.py::pivot_rows` (pure-Python crosstab — no pandas). It cannot touch two sources in one call.

### 1.2 BigQuery BI
- `bigquery.yaml` profile gated on an active `provider='bigquery'` connector. **`.claude/skills/bigquery-bi/SKILL.md` is stale** — it documents a removed multi-agent BI router (`bi_agent.yaml`, `_select_agent`) that no longer exists.
- `bigquery_service.py::execute_query` (read-only validated, `maximum_bytes_billed`=1 GB, 30 s timeout, default `max_rows=1000`, `truncated` flag) + `discover_schema`, `validate_connection`, `estimate_query_cost` (dry-run). Three agent tools (`bigquery_tools.py`): `bigquery_sql` / `schema` / `cost_estimate_execute`. `_BIGQUERY_HINT` warns "Standard SQL, LIMIT not FETCH FIRST… do not confuse with SuiteQL."
- **Seeded BQ schema chunks are vector-invisible in prod** (audit-confirmed): `bigquery_schema_seeder.py` writes `DomainKnowledgeChunk` rows with **no embedding**; `domain_knowledge.py` retrieve filters `embedding.isnot(None)` (l.83). Partitions `bi/common-queries` and `bi/metric-definitions` are *referenced* by `bigquery.yaml` but **never seeded by any code**.

### 1.3 NetSuite SuiteQL
- Two interchangeable paths chosen at **prompt level** (advisory `EXECUTION PRIORITY`, not enforced): LOCAL `netsuite.suiteql` (`netsuite_suiteql.py` → SuiteTalk REST) and EXTERNAL Oracle MCP `ns_runCustomSuiteQL`.
- **Custom records are local-only**: `validate_query` allow-lists `customrecord_*`/`customlist_*`; the hosted MCP resolves standard tables only.
- Dialect rules live in `netsuite.yaml` `<suiteql_dialect_rules>`. Transport: `netsuite_client.py::execute_suiteql` (REST default, paginates at 1000 rows/page). **Audit correction:** the real cap is **`NETSUITE_SUITEQL_MAX_ROWS=50000`** (`config.py:61`) — 1000 is only the per-page size; the pivot path requests 10000.
- `result_cache.py`: Redis per-conversation cache (`CACHE_TTL_SECONDS=1800` / 30 min, `MAX_PREVIEW_ROWS=50`), keyed by `result_type` — **already holds NS + BQ tables side-by-side per conversation.**

### 1.4 Drive RAG
- A working **doc-Q&A** pipeline (PR #73), **not a queryable data source.** Folders → nightly Celery sync (`drive-rag-sync-nightly` 06:00 UTC + manual button) → `drive_rag/indexer.py::sync_folder` chunks/embeds into `drive_chunks` (pgvector `Vector(1024)`, OpenAI text-embedding-3-small).
- Retrieval: `retriever.py::retrieve_drive_chunks` (cosine, `top_k=6`, `min_similarity=0.50`). At chat time `_gather_drive_knowledge`/`_build_drive_knowledge_block` inject a `<drive_knowledge>` prose block (gated on the `google_drive` profile + `context_need ∈ {DATA, DOCS}`, 15 s timeout). `drive_sources` SSE resolves `[source_name]` citations.
- **Spreadsheets are lossy**: `extractors.py::extract_sheet` reads ONLY the tab literally named `Sheet1`, flattens to tab-separated text, runs the prose chunker (800-token/100-overlap) — headers, types, multi-tab structure destroyed.
- **Two important findings:**
  1. **`drive_read_doc` and `docs.create` are dead** — registered in `registry.py` (l.600/621) but **absent from `ALLOWED_CHAT_TOOLS`** (`nodes.py:31`), so `build_local_tool_definitions` filters them out. The agent cannot call them despite `google_drive.yaml` instructing it to.
  2. **`sheets.read_range` / `sheets.create` / `sheets.write_range` ARE callable** (in `ALLOWED_CHAT_TOOLS`). So the agent can already pull a Sheet's cell range as **structured tool output today** — a live, overlooked structured-data seam that bypasses the broken `drive_read_doc` and the lossy RAG ingestion.

### 1.5 Visualization, narrative, skills & prebuilt prompts
- **Visualization is LLM-authored**: the model writes `<chart>JSON</chart>`; `chart_extractor.py::extract_charts` parses post-stream into `schemas/chart.py::ChartData` (7 types) carrying its own inline data; `chart-renderer.tsx` renders via recharts. The chart data is **hand-transcribed by the LLM from the preview, not bound to the `data_table` event.** The `<chart>` instruction lives **only in `bigquery.yaml` step 6** — NetSuite-only and cross-source turns are **never told to chart.**
- **One deterministic chart path**: `financial_chart_builder.py::build_financial_chart`, auto-built at interception — but only for `report_type ∈ {income_statement, balance_sheet}` with `summary.by_period ≥ 2` (`trial_balance`/`trend` get none).
- **No narrative module**: "narration" is one prompt line in `bigquery.yaml` step 7. The insights-oriented `DataAnalysisAgent` prompt is dead code.
- **The no-LLM-numbers rule works** (audit-confirmed): `data_table` standard = `rows[:30]`, FULL-context investigation = ALL rows, `saved_search` = `rows[:5]`, `financial_report` = **zero rows** (summary only). All carry "Do NOT reproduce the table."
- **Prebuilt prompts**: a file-based `SKILL.md` system (`skills/__init__.py`) surfaced as `/` slash commands via `/api/v1/skills/catalog` + `chat-input.tsx`. 4 playbooks (`csv_import_generator`, `inventory_check`, `period_comparison`, `sales_by_platform`) — **all NetSuite-SuiteQL-only, none touch BigQuery/Drive, none chart.** `match_skill` = exact-slash-first-word then substring, first-match-wins, no embeddings. `sales_by_platform` hardcodes tenant-specific `custitem_fw_platform` (prompt pollution).
- **Per-tenant pattern infra** (`query_pattern_service.py` + `tenant_query_patterns`, pgvector) exists but **auto-learning is DISABLED** (CLAUDE.md Known Issues #2, 2026-04-09) — only admin-seeded/nightly-promoted patterns are retrievable, so it's currently a mostly-empty, write-frozen store.

---

## 2. Why Deterministic Cross-Source Joining Is Hard Today

Four compounding, structural reasons:

1. **No join code exists.** "Joining" is two prose strings. There is no service that takes table A + table B and returns A ⋈ B.
2. **The no-LLM-numbers interception actively blocks accurate joining.** The model is asked to "correlate" two tables it can see only ~30 rows of each (zero for financial). Any comparison beyond a handful of rows is approximate / hallucination-prone. *The trust mechanism and the join goal are in direct tension.*
3. **Each result is emitted in isolation and never combined.** Separate `data_table` SSE per source → two independent grids; the session **forgets** the cross-source state (`_compute_source_pin_update` → `None`). There is no persistent "this analysis spans both sources" object to anchor a join on.
4. **Row caps & persistence seams are too small/divergent.** BigQuery default 1000; NetSuite cap 50000 (REST 1000/page); pivot 10000. The only reuse seam (`reference_previous_result` / `result_cache`) caps at **50 rows**, 30-min TTL, one result per message. Nothing materializes a source's rows into a queryable backend store where a real SQL join could run. Row-shape normalization is also duplicated across **three** sites (`netsuite_suiteql.collect_columns`, `_intercept_tool_result` MCP handling, `tool_call_results._extract_items_as_table`) with no canonical normalizer.

**External constraint (verified):** SuiteQL supports inner/left/right/cross joins **only within NetSuite**, caps ~100k rows/query and ~15 concurrent REST requests/account, and **cannot join NetSuite to an external system in a single query.** So cross-source analytics *requires* either pulling bounded result sets and joining app-side, or pre-materializing NetSuite into a warehouse. ([Houseblend — Joining NetSuite ERP & CRM Data with SuiteQL](https://www.houseblend.io/articles/suiteql-join-erp-crm-data))

---

## 3. The Decision — Join-Engine Options Compared

The deliverable was "compare both, you recommend." Here are the four design families with verified tradeoffs, then the recommendation.

### A1 — In-process DuckDB / Polars (pull-and-join in the backend) ✅ *recommended backbone*
Fetch each source into Apache Arrow once, then run plain SQL that JOINs the frames by name via DuckDB **replacement scans** — zero-copy, filter/projection pushdown, out-of-core spill for big joins. DuckDB can also `ATTACH` Postgres/MySQL/SQLite and read BigQuery via `bigquery_scan()`/`bigquery_query()`. DuckDB ≈ Polars-streaming (within ~20% on join-heavy benchmarks); both beat pandas/Spark/Dask 15–94×. ([DuckDB Quacks Arrow](https://duckdb.org/2021/12/03/duck-arrow), [Multi-DB Support](https://duckdb.org/2024/01/26/multi-database-support-in-duckdb), [MotherDuck — the great federator](https://motherduck.com/blog/duckdb-the-great-federator/), [Polars PDS-H](https://pola.rs/posts/benchmarks/))

- **Why it fits us best:** `pivot_service.pivot_rows` + `pivot_tool.py` already prove the *re-execute-then-shape* pattern for both dialects; `result_cache` already holds NS + BQ tables side-by-side; `_intercept_tool_result` already emits a normalized `{columns, rows}` — schema-unification is half-done. We don't ATTACH live warehouses; we join the **already-fetched, already-cached** result sets in memory.
- **Hard rules (must respect):** cross-database pushdown is weak → **pre-filter each side** before the join; set `memory_limit` conservatively (the 80%-of-RAM default is too high on the e2-small VM) with a writable `temp_directory` so spilling works; treat DuckDB as **per-request, per-process ephemeral** compute — `DuckDBPyConnection` is not thread-safe, never share one writable on-disk file across FastAPI/Celery workers. ([Memory Management](https://duckdb.org/2024/07/09/memory-management), [Concurrency](https://duckdb.org/docs/current/connect/concurrency), [Postgres scanner](https://duckdb.org/2022/09/30/postgres-scanner))

### A2 — Federated / virtualized query (Trino/Presto-class) ❌ *rejected for now*
One SQL interface over heterogeneous sources, MPP execution; governance (access control + audit) must be bolted on externally (e.g. Apache Ranger). **Over-scoped** for a single tenant joining a few BigQuery tables to NetSuite; perf is bound by the slowest source + network. ([Trino + Ranger — AWS](https://aws.amazon.com/blogs/big-data/enable-federated-governance-using-trino-and-apache-ranger-on-amazon-emr/))

### A3 — Semantic-layer grounding (metrics-as-tools) ✅ *additive, later*
Model high-value metrics + canonical join paths once; the LLM only selects named metrics/dimensions and a deterministic engine compiles the SQL. A 2026 dbt benchmark reports routing through the dbt Semantic Layer/MetricFlow lifted accuracy vs raw text-to-SQL — Claude Sonnet 4.6 90.0%→98.2%, GPT-5.3 84.1%→100.0% — and converts silent-wrong answers into explicit errors. ([dbt 2026 benchmark](https://docs.getdbt.com/blog/semantic-layer-vs-text-to-sql-2026))

- **Verified caveat (do not over-read):** those are the **with-modeled-data** numbers. On un-modeled entity hops the semantic layer scored **0%** (explicit error) while text-to-SQL still *attempted* them. The gain is conditional on modeling labor → **ship a hybrid**: governed metrics for KPIs/board numbers + a DuckDB/text-to-SQL escape hatch for ad-hoc. The "can't be subtly wrong" guarantee covers SQL *generation* only, not metric selection or narration.
- Production options already speak **MCP** (Cube ships an MCP server; MetricFlow is Apache-2.0, anchors the Open Semantic Interchange) — matching our `ns_*` MCP pattern, so we can graduate cleanly later. ([Cube](https://cube.dev/), [MetricFlow OSS](https://www.getdbt.com/blog/open-source-metricflow-governed-metrics), [MCP + semantic layer](https://colrows.com/blogs/mcp-semantic-layer-integration/))

### A4 — Pure-LLM multi-step orchestration (TAG) ⚠️ *keep the shape, not as the join engine*
Table-Augmented Generation (synthesize query → execute deterministically → LLM narrates retrieved rows) beats text-to-SQL/RAG on complex questions (~55–65% vs <20%). ([TAG, arXiv 2408.14717](https://arxiv.org/html/2408.14717v1)) **But** multi-relational joins crater LLM accuracy generally (~10% on Spider 2.0 vs ~86% single-query), and agents flip correct→wrong under one round of pushback. ([DS-agent survey, arXiv 2510.04023](https://arxiv.org/html/2510.04023v1)) → **We keep TAG's "execute deterministically, narrate only" shape** (which `_intercept_tool_result` already embodies) and make DuckDB the deterministic execution target for the join step. We do **not** rely on the LLM to do the join itself.

### Recommendation
**A1 DuckDB as the deterministic backbone now; A3 metric catalog layered on top, starting lightweight in-repo and graduating to Cube/MetricFlow-over-MCP later; A2 rejected; A4's pattern retained for narration only.** This is the lowest-lift, highest-fit path: it matches the user's instinct ("joining done using the backend"), reuses the most existing code, and *resolves* rather than fights the no-LLM-numbers trust boundary.

---

## 4. Recommended Architecture

**Unifying invariant:** *the deterministic backend does all computation (join, pivot, stat, chart); the LLM orchestrates and narrates, and never emits a computed number.* This is the existing per-source rule, extended cross-source.

### 4.1 Deterministic cross-source compute — DuckDB engine + `cross_source_query` tool
- New backend service wrapping an **ephemeral per-request DuckDB connection**. Inputs: bounded result sets already in `result_cache` (NS, BQ, Sheets) referenced by id/name; a join key + the requested join/aggregate/pivot. Output flows back through `_intercept_tool_result` → `data_table` SSE (LLM never sees the full joined rows; it narrates the preview/summary only).
- New agent tool (e.g. `cross_source_query` / `join_results`) added to the registry + `ALLOWED_CHAT_TOOLS`. **Replace `cross_source.yaml` step 3** ("correlate in prose") with "call the join tool with the two cached results + join key."
- Generalize `pivot_service`/`pivot_tool` into this engine so single-source pivot and cross-source join share one normalized `{columns, rows}` path — and collapse the **three** duplicate row-normalizers into one canonical normalizer.
- Respect the hard rules from §3-A1 (pre-filter, conservative `memory_limit` + `temp_directory`, per-request connection).

### 4.2 Governed metric catalog (grounds KPIs + prebuilt prompts)
- A lightweight **in-repo metric registry** (YAML, sibling to knowledge profiles): named metrics (aggregation formula), dimensions, synonyms, canonical join paths per source. The LLM selects a named metric; the backend compiles deterministic SQL. Seed the already-referenced-but-empty **`bi/metric-definitions`** partition.
- Each prebuilt prompt maps **1:1 to a governed metric** → "just-use-it" prompts are guaranteed-correct. (Grounding gen-AI in a semantic layer is reported to cut query errors materially, though vendor figures like Looker's ~66% are self-reported — corroborate with our own benchmark. [Looker](https://cloud.google.com/blog/products/business-intelligence/how-lookers-semantic-layer-enhances-gen-ai-trustworthiness))
- Keep a **text-to-SQL/DuckDB escape hatch** for ad-hoc questions the catalog doesn't model (per the verified 0%-on-unmodeled caveat).

### 4.3 Drive-as-data — two tracks
- **Structured track (quick win):** surface the **already-callable `sheets.read_range`** so a Drive Sheet becomes a **joinable table** flowing through the same cache + interception + DuckDB path. Fix the Sheet1-only lossy extractor (header-preserving, multi-tab, route tabular data to structured not prose). ([Firecrawl chunking](https://www.firecrawl.dev/blog/best-chunking-strategies-rag), [Paragon — Drive RAG](https://www.useparagon.com/learn/what-to-know-about-ingesting-google-drive-data-for-rag/))
- **Awareness track:** build a **document/source catalog** — a queryable inventory (title/owner/dates/type/folder) surfaced to the LLM as an `<available_sources>` block so it *knows what Drive data exists* before retrieval (reuse the `#`-picker's `GET /drive-folders/files`). Discovery-precedes-extraction. ([Document tooling layer](https://medium.com/@markbabcock_79883/designing-a-document-tooling-layer-for-agent-workflows-da777a8651ce) — single-author proposal, corroborated by vendor guidance)
- **Fixes:** enable (or intentionally retire) the dead `drive_read_doc`/`docs.create`; fix NULL-embedding schema chunks; move to incremental sync (Drive v3 Changes API + `startPageToken`). Keep prose-RAG `<drive_knowledge>` for *qualitative narrative grounding*.

### 4.4 Deterministic visualization
- Generalize `financial_chart_builder.build_financial_chart` into a **"given this `data_table`, build the right chart"** path: backend chart-type recommendation bound to the **actual rows** (not LLM-transcribed numbers), emitted as `ChartData` through the existing `chart` SSE + `chart-renderer.tsx`.
- Extend charting instruction beyond `bigquery.yaml` to NetSuite + cross-source turns. Add a chart-spec **validator**. (Code-gen/structured-intent charts score ~92–95% vs ~70% for hand-written Vega-Lite specs — generate structured intent + validate, don't free-write specs. [arXiv 2507.22890](https://arxiv.org/html/2507.22890v1), small-sample caveat)
- *Reliability fix:* charts are emitted **only post-stream**, so an aborted turn drops the chart — worth addressing.

### 4.5 Financial storytelling grounded on computed numbers
- Add a **deterministic stat/insight service** (extend `pivot_service`): compute trends/variance/drivers/outliers as **FACTS**; the LLM narrates over those ground truths with magnitudes + drivers + **citations** back to source rows. This is the Tableau-Pulse / Power-BI-Smart-Narrative split and extends our no-numbers boundary to *insights*. ([Tableau Pulse](https://www.tableau.com/products/tableau-pulse), [Power BI AI 2026](https://powerbiconsulting.com/blog/power-bi-ai-machine-learning-features-guide-2026), [ChartCitor — cell-level attribution, arXiv 2502.00989](https://arxiv.org/pdf/2502.00989))

### 4.6 Prebuilt prompt library
- Extend the **existing `SKILL.md` slash-command system** to: (a) cover BigQuery + Drive + cross-source + charts (today all 4 are SuiteQL-only); (b) map 1:1 to governed metrics (§4.2); (c) become **per-tenant** (today global file-system) — optionally revive `tenant_query_patterns` as the per-tenant store; (d) add empty-state **starter chips**. De-pollute `sales_by_platform` (hardcoded `custitem_fw_platform`).
- Adopt the "verified query repository doubles as accuracy gate" idea — each prebuilt prompt becomes a benchmark question. ([Snowflake Cortex Analyst](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst), [Databricks Genie trusted assets](https://docs.databricks.com/aws/en/genie/trusted-assets))

---

## 5. Phased Roadmap

Each phase is independently shippable, TDD'd, and gated by the existing vs-Claude+MCP benchmark.

| Phase | Goal | Key scope / files | Why it ships alone |
|------|------|-------------------|--------------------|
| **0 — Fix broken seams** | Stop silent dead-ends | Enable or retire `drive_read_doc`/`docs.create` (`nodes.py` `ALLOWED_CHAT_TOOLS` vs `registry.py`); embed the BQ schema chunks (`bigquery_schema_seeder.py` / `domain_knowledge.py` l.83); correct stale `bigquery-bi/SKILL.md`; de-pollute `sales_by_platform` | Pure cleanup, low risk, unblocks later phases |
| **1 — DuckDB cross-source engine** ⭐ | Real backend joins/pivots | New compute service + `cross_source_query` tool; feed from `result_cache`; output via `_intercept_tool_result`; rewrite `cross_source.yaml` step 3; unify the 3 row-normalizers | **Fixes the #1 pain directly**; reuses cache + interception |
| **2 — Metric catalog + prebuilt prompts** | Trustworthy KPIs + self-serve | Seed `bi/metric-definitions`; in-repo metric YAML registry; extend `SKILL.md` library across sources + map to metrics; starter chips | Each metric/prompt is independently valuable + benchmarkable |
| **3 — Drive-as-data** | Drive becomes joinable + the agent knows it exists | `sheets.read_range` → cached joinable table; header-preserving extractor; document/source catalog + `<available_sources>`; incremental sync | Quick win (tool already callable) + awareness layer |
| **4 — Deterministic viz + narrative** | Trustworthy charts + storytelling | Generalize `financial_chart_builder`; chart-type rec + validator; deterministic stat/insight service + grounded narration w/ citations | Removes LLM-number-transcription bug; extends coverage |
| **5 — Graduate (optional)** | Scale governance | External semantic layer (Cube/MetricFlow over MCP) if modeling demand grows; chart vision-score regression in CI; revive tenant pattern auto-learning | Additive; only if Phase 2's lightweight catalog hits limits |

---

## 6. Risks, Caveats & Open Questions

- **DuckDB operational discipline is non-negotiable** (per-request ephemeral connection, conservative `memory_limit` + writable `temp_directory`, pre-filter before join) — especially on the e2-small staging VM. Get this wrong → OOM.
- **Semantic-layer ROI is conditional on modeling labor** — un-modeled questions score 0%. Hence the hybrid + escape hatch. Don't sell the dbt 90→98% numbers as out-of-box.
- **NetSuite limits** (~100k rows/query, ~15 concurrent REST, no external joins) bound how much we can pull per side — reinforces pre-filtered, bounded result sets.
- **Trust boundary must hold across the new compute** — the joined/charted/narrated output must keep routing through `_intercept_tool_result` so the LLM still never emits raw computed numbers.
- **Charts emit post-stream only** — truncated turns drop them; address in Phase 4.
- **Several cited figures are vendor-reported or small-sample** (Looker ~66%, viz ~92–95%, TAG ~65%) — directional; corroborate with our own benchmark, which we already run.
- **Open questions for the spec phase:** Where does the joined result get cached (extend `result_cache` row cap beyond 50?)? Does `cross_source_query` take raw cached-result-ids or re-run both queries? How big can a single in-memory join get before we must push down to BigQuery? Per-tenant metric registry storage (YAML files vs DB table)? RLS/tenant-isolation for the DuckDB layer.

---

## Appendix A — Citations

Cross-source / federation: [Houseblend SuiteQL joins](https://www.houseblend.io/articles/suiteql-join-erp-crm-data) · [DuckDB Quacks Arrow](https://duckdb.org/2021/12/03/duck-arrow) · [DuckDB Multi-DB](https://duckdb.org/2024/01/26/multi-database-support-in-duckdb) · [MotherDuck federator](https://motherduck.com/blog/duckdb-the-great-federator/) · [Polars PDS-H](https://pola.rs/posts/benchmarks/) · [DuckDB Memory Mgmt](https://duckdb.org/2024/07/09/memory-management) · [DuckDB Postgres scanner](https://duckdb.org/2022/09/30/postgres-scanner) · [DuckDB Concurrency](https://duckdb.org/docs/current/connect/concurrency) · [Trino + Ranger (AWS)](https://aws.amazon.com/blogs/big-data/enable-federated-governance-using-trino-and-apache-ranger-on-amazon-emr/) · [TAG (arXiv 2408.14717)](https://arxiv.org/html/2408.14717v1) · [DS-agent survey (arXiv 2510.04023)](https://arxiv.org/html/2510.04023v1)

Semantic layer / metrics: [dbt 2026 benchmark](https://docs.getdbt.com/blog/semantic-layer-vs-text-to-sql-2026) · [Cube](https://cube.dev/) · [Atlan semantic tools](https://atlan.com/know/best-semantic-layer-tools/) · [MetricFlow OSS](https://www.getdbt.com/blog/open-source-metricflow-governed-metrics) · [MCP + semantic layer (Colrows)](https://colrows.com/blogs/mcp-semantic-layer-integration/) · [Looker gen-AI grounding](https://cloud.google.com/blog/products/business-intelligence/how-lookers-semantic-layer-enhances-gen-ai-trustworthiness) · [Looker trusted metrics](https://cloud.google.com/blog/products/data-analytics/grounding-analytical-ai-agents-with-lookers-trusted-metrics) · [Querio NLQ BI 2026](https://querio.ai/articles/natural-language-query-business-intelligence-thoughtspot-vs-power-bi-vs-tableau-2026)

Drive RAG / source awareness: [Document tooling layer](https://medium.com/@markbabcock_79883/designing-a-document-tooling-layer-for-agent-workflows-da777a8651ce) · [Cost-efficient agentic RAG (TDS)](https://towardsdatascience.com/building-cost-efficient-agentic-rag-on-long-text-documents-in-sql-tables/) · [Firecrawl chunking](https://www.firecrawl.dev/blog/best-chunking-strategies-rag) · [Paragon Drive RAG](https://www.useparagon.com/learn/what-to-know-about-ingesting-google-drive-data-for-rag/) · [LlamaIndex live Drive RAG](https://developers.llamaindex.ai/python/examples/ingestion/ingestion_gdrive/) · [Unstructured RAG challenges](https://unstructured.io/insights/rag-pipeline-challenges-from-data-ingestion-to-retrieval) · [Tool RAG (Red Hat)](https://next.redhat.com/2025/11/26/tool-rag-the-next-breakthrough-in-scalable-ai-agents/) · [CSR-RAG (arXiv 2601.06564)](https://arxiv.org/html/2601.06564)

Visualization: [LLM viz eval (arXiv 2507.22890)](https://arxiv.org/html/2507.22890v1) · [NL2VIS eval (arXiv 2401.11255)](https://arxiv.org/abs/2401.11255) · [VegaChat (arXiv 2601.15385)](https://arxiv.org/html/2601.15385v1)

Financial narrative: [Tableau Pulse](https://www.tableau.com/products/tableau-pulse) · [Tableau Pulse + AI](https://www.tableau.com/blog/tableau-pulse-and-tableau-ai) · [Power BI AI 2026](https://powerbiconsulting.com/blog/power-bi-ai-machine-learning-features-guide-2026) · [ChartCitor (arXiv 2502.00989)](https://arxiv.org/pdf/2502.00989) · [Data-to-Dashboard (arXiv 2505.23695)](https://arxiv.org/html/2505.23695v1) · [Ema financial LLMs](https://www.ema.co/additional-blogs/addition-blogs/financial-llm-in-finance-programs)

Prebuilt prompts / catalog: [Genie trusted assets](https://docs.databricks.com/aws/en/genie/trusted-assets) · [Genie setup](https://docs.databricks.com/aws/en/genie/set-up) · [Genie best practices](https://docs.databricks.com/aws/en/genie/best-practices) · [Cortex Analyst](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst)

## Appendix B — Research Provenance

Hybrid codebase+web harness, run `wf_87042a2f-989` (2026-06-03): 5 read-only code mappers (cross-source orchestration, BigQuery BI, NetSuite SuiteQL, Drive RAG, viz/narrative/skills) + 5 web angles each adversarially verified (2/3-refute kills) + a code-grounding audit that corrected 4 material claims (notably the 50k NetSuite cap and the zero-row financial interception) + a neutral synthesizer. 17 agents, ~1.37M tokens, 345 tool calls, ~12.7 min. Strategic recommendations and roadmap (§3–§6) authored on the main thread, not by subagents.
