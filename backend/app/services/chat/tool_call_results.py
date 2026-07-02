from __future__ import annotations

import json
from typing import Any

# Persistence-boundary row cap (re-gate r3, finding #6): the FULL pre-truncation
# result is frozen into ChatMessage.tool_calls[].result_payload (JSONB) AND the
# in-turn full-payload sidecar. A broad SuiteQL turn can return up to
# NETSUITE_SUITEQL_MAX_ROWS (50k) rows, which would bake multi-MB JSONB into a
# single Postgres row (risking the Supabase 2-min INSERT timeout) on an ORDINARY
# turn. This is the STORAGE cap; report rendering curates much further (report_service
# keeps only the first _REPORT_TABLE_TOP_K rows per table, charts at 100). We cap the
# STORED rows here while keeping the TRUE row_count + truncated=True so render_report_html
# still discloses the truncation.
MAX_STORED_PAYLOAD_ROWS = 2000


def _cap_stored_rows(rows: list, row_count: int, truncated: bool) -> tuple[list, int, bool]:
    """Cap a frozen payload's rows at ``MAX_STORED_PAYLOAD_ROWS``, preserving the
    TRUE ``row_count`` and forcing ``truncated=True`` when a cap is applied."""
    if len(rows) > MAX_STORED_PAYLOAD_ROWS:
        return rows[:MAX_STORED_PAYLOAD_ROWS], row_count, True
    return rows, row_count, truncated


def is_stamped_data_tool(tool_name: str) -> bool:
    """True for tools whose result the intercept STAMPS a result_id into (so the
    LLM SEES the id): financial reports, data tables (SuiteQL/BigQuery/pivot/metric),
    and saved searches.

    This is condition (b) of the UNIFIED SLOT CRITERION (re-gate r3): a result gets
    an r-id slot — and thus a persisted ``result_payload`` + a sidecar — IFF it is
    extractable (condition a) AND the intercept stamps it (this predicate). It MUST
    mirror ``orchestrator._is_financial_tool`` / ``_is_data_table_tool`` /
    ``_is_saved_search_tool`` so a payload-bearing but hidden/'other'-category tool
    (ns_listAllReports) is excluded from BOTH the in-turn numbering and the
    persisted-fallback population — keeping the two id spaces dense + aligned.
    Lives here (not orchestrator) so ``build_tool_call_log_entry`` can gate
    persistence on the same predicate without a circular import.
    """
    from app.services.chat.tool_categories import categorize

    category = categorize(tool_name)
    if category in ("financial", "data_table", "bigquery"):
        return True
    lowered = tool_name.lower()
    if "savedsearch" in lowered or "runsavedsearch" in lowered:
        return True
    return tool_name in ("netsuite.saved_search", "netsuite_saved_search")


def parse_tool_result_value(result_value: Any) -> dict[str, Any]:
    """Best-effort parse of a tool result payload or summary string."""
    if isinstance(result_value, dict):
        return result_value
    if not isinstance(result_value, str):
        return {}
    try:
        parsed = json.loads(result_value)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def get_tool_call_result_data(tool_call: dict[str, Any]) -> dict[str, Any]:
    """Return the most structured result representation available for a tool call."""
    result_payload = tool_call.get("result_payload")
    if isinstance(result_payload, dict):
        return result_payload
    return parse_tool_result_value(tool_call.get("result_summary"))


def tool_call_had_error(tool_call: dict[str, Any]) -> bool:
    data = get_tool_call_result_data(tool_call)
    error = data.get("error")
    return error is True or (isinstance(error, str) and bool(error.strip()))


def tool_call_row_count(tool_call: dict[str, Any]) -> int:
    data = get_tool_call_result_data(tool_call)
    row_count = data.get("row_count")
    if isinstance(row_count, int):
        return row_count

    rows = data.get("rows")
    if isinstance(rows, list):
        return len(rows)

    items = data.get("items")
    if isinstance(items, list):
        return len(items)

    return 0


def summarize_tool_result(tool_name: str, result_str: str) -> str:
    """Build a compact user-facing summary for persisted tool call logs."""
    parsed = parse_tool_result_value(result_str)

    if parsed:
        error_message = _extract_error_message(parsed)
        if error_message:
            return error_message[:500]

        # workspace_propose_patch's result carries the changeset_id the frontend
        # needs to render ChangeProposalCard's Approve/Apply buttons. Don't
        # collapse it to "Returned 1 row" — return an allowlisted JSON payload
        # so parseResult() in change-proposal-card.tsx can extract changeset_id.
        #
        # CRITICAL: the raw propose_patch result includes diff_preview with up
        # to 32KB of original_content + modified_content (see workspace_service
        # .propose_patch). SuiteScripts can contain credentials, tokens,
        # internal IDs, customer data, or business logic. Persisting that into
        # ChatMessage.tool_calls — which is replayed into LLM history and
        # shipped to the frontend — would leak file contents downstream.
        # Allowlist action-relevant fields only; the frontend already has the
        # diff in step.params.unified_diff.
        if tool_name == "workspace_propose_patch" and parsed.get("changeset_id"):
            return json.dumps(
                {
                    "changeset_id": parsed["changeset_id"],
                    "patch_id": parsed.get("patch_id", ""),
                    "operation": parsed.get("operation", "modify"),
                    "diff_status": parsed.get("diff_status", "unknown"),
                    "risk_summary": parsed.get("risk_summary", ""),
                }
            )

    # Try to compute a row count from any known shape
    row_count: int | None = None
    if isinstance(parsed, dict):
        row_count = parsed.get("row_count") if isinstance(parsed.get("row_count"), int) else None
        if row_count is None and isinstance(parsed.get("count"), int):
            row_count = parsed["count"]
        if row_count is None:
            for key in ("rows", "items"):
                collection = parsed.get(key)
                if isinstance(collection, list):
                    row_count = len(collection)
                    break

    # Top-level list (e.g. ns_listAllReports returns [...])
    if row_count is None and isinstance(result_str, str):
        try:
            top_level = json.loads(result_str)
            if isinstance(top_level, list):
                row_count = len(top_level)
        except (json.JSONDecodeError, TypeError):
            pass

    if row_count is None:
        return result_str[:500]

    suffix = ""
    if isinstance(parsed, dict) and (parsed.get("truncated") or parsed.get("rows_truncated")):
        suffix = " (truncated)"
    if row_count == 0:
        return f"No rows returned{suffix}"
    return f"Returned {row_count} row{'s' if row_count != 1 else ''}{suffix}"


def _extract_items_as_table(parsed: dict[str, Any] | list) -> tuple[list[str], list[list]] | None:
    """Extract columns/rows from a list-of-dicts response (MCP SuiteQL, saved searches, etc.).

    Handles:
      - {"items": [{...}, ...]} — ns_runCustomSuiteQL, ns_runSavedSearch
      - {"data": [{...}, ...]} — external MCP ns_runCustomSuiteQL (chat-orchestration
        rule #3: external MCP returns ``{"data": [{col: val}], ...}``, NOT columns/rows).
        The interceptor's data_table branch (orchestrator._intercept_tool_result) treats
        this top-level ``data`` key as a data result and stamps a result_id, so the
        payload extractor MUST recognize the same shape — otherwise the SINGLE
        id-assignment criterion (payload non-None) never fires for this (most common
        NetSuite) data source and report.compose can't resolve the stamped id.
      - [{...}, ...] — ns_listAllReports, ns_listSavedSearches (top-level list)
      - {"reportData": {...}} — ns_runReport (hierarchical, handled separately)
    """
    items: list[dict] | None = None

    if isinstance(parsed, list):
        items = parsed
    elif isinstance(parsed, dict):
        # Prefer "items"; fall back to the external-MCP "data" key (same shape).
        items_val = parsed.get("items")
        if not isinstance(items_val, list):
            items_val = parsed.get("data")
        if isinstance(items_val, list):
            items = items_val

    if not items or not isinstance(items[0], dict):
        return None

    # Derive columns from all items (union of keys, preserving first-seen order)
    seen: set[str] = set()
    columns: list[str] = []
    for item in items:
        for key in item:
            if key not in seen:
                seen.add(key)
                columns.append(key)

    # Build rows aligned to columns
    rows = [[item.get(col) for col in columns] for item in items]
    return columns, rows


def _coerce_netsuite_bool(value) -> bool | None:
    """NetSuite serializes booleans as JSON true/false OR the string ``"T"``/``"F"`` (a
    pervasive convention — cf. suiteql_validator / prompt_template_service). Coerce both;
    return None for absent/unrecognized so the caller can fall back to inference. A bare
    ``bool("F")`` is True (both strings are truthy), which would silently invert the
    hierarchy signal. Named NetSuite-explicitly: ``pricing_tools._coerce_bool`` is a
    SAME-NAMED coercer with the OPPOSITE result for ``"T"`` (its truthy set is
    {"true","1","yes"}) — a generic name here invites a copy-paste that silently flips
    every statement line's hierarchy."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("t", "true", "1", "yes"):
            return True
        if s in ("f", "false", "0", "no"):
            return False
    return None


def _line_hierarchy(entry: dict, value_source: str | None) -> dict:
    """Per-row statement-hierarchy metadata carried ALONGSIDE the flattened
    ``[account, amount]`` row (never as a column), so a later curation step
    (``report.compose`` Phase 3) can build a curated statement / key-figure callouts
    from the section structure the flat table otherwise discards.

    ``is_summary``: a summary/section/total line vs an individual detail account.
    NetSuite marks this authoritatively with ``isDetailLine`` (coerced — it may arrive as
    JSON false OR the string ``"F"``). When that key is absent, fall back to the SAME
    non-empty value list the amount was read from (``value_source``: ``"summary"`` vs
    ``"detail"``) — NOT mere key presence, which would mislabel a detail line that merely
    carries an empty ``summaryLineValues`` key. ``level``: indent depth
    (``indentLevel``/``indent``/``level``; via ``float`` so ``"2.0"`` parses), 0 when
    unknown. All structural, keyed off reportData markers — no hardcoded account/label
    names (no prompt pollution).
    """
    is_detail = _coerce_netsuite_bool(entry.get("isDetailLine"))
    if is_detail is None:
        is_detail = value_source == "detail"  # matches amount extraction's non-emptiness
    level = 0
    for key in ("indentLevel", "indent", "level"):
        # Fall THROUGH a present-but-unparseable key ({"indentLevel": null, "level": 2}
        # must yield 2, not stop at the null) — take the first key that parses.
        if key in entry:
            try:
                level = int(float(entry[key]))
                break
            except (TypeError, ValueError):
                continue
    return {"is_summary": not is_detail, "level": level}


def _extract_report_data_as_table(report_data: dict) -> tuple[list[str], list[list], list[dict]] | None:
    """Flatten ns_runReport hierarchical reportData into ``(columns, rows, line_meta)``.

    Columns are ``["account", "amount"]`` — the human-readable line label and its
    amount. The detail/section line-type marker is intentionally NOT emitted as a
    COLUMN: as the first column it would become a chart's x-axis (report.compose keys
    the x-axis off the first column) and bury the account names under repeated
    "detail"/"section" labels. Instead the hierarchy travels as ``line_meta`` — a list
    of ``{is_summary, level}`` dicts PARALLEL to ``rows`` — so report.compose (Phase 3)
    can curate a statement / key-figure callouts without the marker polluting the table.

    The ``rows`` themselves are UNCHANGED (faithful): every figure is preserved (the
    "never drop a figure" invariant); curation of blanks/detail/placeholder is Phase 3's
    job off ``line_meta``, not this faithful flatten's.
    """
    if not isinstance(report_data, dict) or not report_data:
        return None

    columns = ["account", "amount"]
    rows: list[list] = []
    line_meta: list[dict] = []

    for _key, entry in sorted(report_data.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 0):
        if not isinstance(entry, dict):
            continue
        label = entry.get("value") or entry.get("label") or ""
        # Get amount from summaryLineValues or detailLineValues, remembering WHICH
        # non-empty list supplied it (value_source) so the hierarchy classifier keys off
        # the same signal — never mere key presence.
        amount = None
        value_source = None
        for vals_key in ("summaryLineValues", "detailLineValues"):
            vals = entry.get(vals_key)
            if isinstance(vals, list) and vals:
                first = vals[0]
                if isinstance(first, dict):
                    # NOT `first.get("Amount") or first.get("amount")`: a legitimate
                    # zero balance ({"Amount": 0}) is falsy, so `or` would drop a real
                    # $0 line to None (common in P&L / balance sheets). Preserve 0 but
                    # still cross-fall to the lowercase key when capital `Amount` is
                    # absent OR present-but-None ({"Amount": null, "amount": 5} → 5).
                    amount = first.get("Amount")
                    if amount is None:
                        amount = first.get("amount")
                    value_source = "summary" if vals_key == "summaryLineValues" else "detail"
                    break
        label_str = str(label).strip()
        # NEVER silently drop a figure from a financial surface. Keep every row that
        # carries a label OR a real amount (including $0 and a blank-label line that
        # happens to repeat the prior amount); drop ONLY a truly-empty row (no label
        # AND no amount). A value-based "duplicate" dedup is unsafe — two genuinely
        # distinct lines can coincide in amount (e.g. two $0 balance-sheet lines), so it
        # would drop a real figure and the total would stop footing. Cosmetic
        # blank-label continuation rows are consolidated by the Phase 3 curated-statement
        # restructure (off line_meta), which has the structure to do it safely.
        # No hardcoded label filtering either — a tenant may name a line anything.
        if not label_str and amount is None:
            continue
        rows.append([label_str, amount])
        line_meta.append(_line_hierarchy(entry, value_source))  # parallel to rows (same keep/skip)

    return (columns, rows, line_meta) if rows else None


def report_data_to_capped_table(report_data: dict) -> tuple[list[str], list[list], list[dict], int, bool] | None:
    """Flatten a hierarchical ``reportData`` dict AND cap it at ``MAX_STORED_PAYLOAD_ROWS``.

    Returns ``(columns, capped_rows, capped_line_meta, true_row_count, truncated)`` or
    None when the reportData has nothing to flatten. The TRUE pre-cap ``row_count`` is
    preserved, and ``line_meta`` is capped in LOCKSTEP with ``rows`` so the two stay
    aligned (the cap only ever truncates the tail).

    SINGLE SOURCE OF TRUTH for BOTH consumers of the reportData shape (re-review #2):
    the persistence path ``extract_result_payload`` Path 2 AND the in-turn intercept
    (orchestrator's ns_runReport branch). Routing both through this one helper makes
    the persist/intercept PARITY structural — the persisted/sidecar table and the
    live-rendered SSE table can never drift on flatten or cap policy."""
    flattened = _extract_report_data_as_table(report_data)
    if flattened is None:
        return None
    columns, rows, line_meta = flattened
    rows, row_count, truncated = _cap_stored_rows(rows, len(rows), False)
    if len(line_meta) > len(rows):  # only when the cap truncated — lockstep, stays aligned
        line_meta = line_meta[: len(rows)]
    return columns, rows, line_meta, row_count, truncated


# Normalized (alphanumeric-only, lowercased) column names that denote money.
_MONEY_COLUMN_EXACT = frozenset({"debit", "credit", "subtotal", "totaldebit", "totalcredit"})
# A column whose normalized name ENDS in "amount"/"balance" is money. This catches the
# canonical NetSuite/SuiteQL line-amount names ("netamount", "foreignamount"), compound
# names ("stripe_amount" → "stripeamount", "opening_balance"), and the bare words —
# WITHOUT the substring false-positives that would tag a count ("total_count"),
# a status ("creditstatus"), or a code as currency.
_MONEY_COLUMN_SUFFIXES = ("amount", "balance")


def _money_columns(columns: list) -> list:
    """Return the subset of ``columns`` whose name denotes a monetary value, so the
    report renderer accounting-formats ONLY those.

    Name-based (NOT value-type), deliberately precise: a column matches iff its
    normalized name ends in "amount"/"balance" or is one of a few exact money words.
    This catches netamount/foreignamount/*_amount/*_balance while NEVER tagging a numeric
    account-code / year / count / id (a substring match or the broad export classifier
    would mis-format "total_count" as "1,500.00"). Conservative under-tagging of an
    ambiguous bare name (e.g. "total"/"revenue", a pivoted per-period column) is
    render-safe — it renders raw, not wrong — see issue #146.
    """
    out = []
    for c in columns:
        name = "".join(ch for ch in str(c).lower() if ch.isalnum())
        if name in _MONEY_COLUMN_EXACT or name.endswith(_MONEY_COLUMN_SUFFIXES):
            out.append(c)
    return out


def extract_result_payload(tool_name: str, params: dict[str, Any], result_str: str) -> dict[str, Any] | None:
    """Attach structured query results for UI rendering when available.

    Handles local netsuite_suiteql (columns/rows format) and external MCP tools
    (items list-of-dicts, reportData hierarchical).
    """
    parsed = parse_tool_result_value(result_str)

    # Handle top-level list (ns_listAllReports, ns_listSavedSearches)
    if not parsed and isinstance(result_str, str):
        try:
            top_level = json.loads(result_str)
            if isinstance(top_level, list) and top_level and isinstance(top_level[0], dict):
                parsed = top_level  # type: ignore[assignment]
        except (json.JSONDecodeError, TypeError):
            pass

    if not parsed:
        return None

    # Reject a FAILED result before extracting ANY table: an `error` key (true / non-empty
    # string) OR an explicit `success: false`. A non-isError MCP body that self-declares
    # `success: false` while still carrying a stale/partial `reportData` scaffold must NOT
    # be persisted as a 'success' table (T2 re-review #1). The intercept guard mirrors this
    # so persist + intercept stay in parity (both reject it).
    if isinstance(parsed, dict) and (_extract_error_message(parsed) or parsed.get("success") is False):
        return None

    # --- Path 0: financial report (netsuite_financial_report / ext ns_runReport) ---
    # The financial_report intercept branch STAMPS a result_id on EVERY successful
    # result — including a zero-activity period (items=[]) — so the SAME unified
    # criterion (extractable payload) MUST produce a non-None payload here, or the
    # stamped id would dangle in report.compose (re-gate r3, finding #5). The
    # financial shape carries ``items`` (list-of-dicts) + a ``summary`` dict, NOT a
    # top-level ``rows`` key, so Path 1 misses it and Path 3 bails on empty items.
    if (
        isinstance(parsed, dict)
        and parsed.get("success") is True
        and isinstance(parsed.get("summary"), dict)
        and "report_type" in parsed
    ):
        fin_cols = parsed.get("columns")
        items = parsed.get("items")
        if not isinstance(items, list):
            items = []
        # Build columns: prefer the declared columns; else union of item keys.
        if isinstance(fin_cols, list) and fin_cols:
            columns = list(fin_cols)
        else:
            seen: set[str] = set()
            columns = []
            for item in items:
                if isinstance(item, dict):
                    for key in item:
                        if key not in seen:
                            seen.add(key)
                            columns.append(key)
        rows = [[item.get(col) for col in columns] if isinstance(item, dict) else list(item) for item in items]
        row_count = parsed.get("total_rows")
        if not isinstance(row_count, int):
            row_count = len(rows)
        rows, row_count, truncated = _cap_stored_rows(rows, row_count, False)
        return {
            "kind": "table",
            "columns": columns,
            "rows": rows,
            "row_count": row_count,
            "truncated": truncated,
            "query": f"{parsed.get('report_type', 'report')} ({parsed.get('period', '')})".strip(),
            "limit": len(rows),
            "currency_columns": _money_columns(columns),
        }

    # --- Path 1: Already has columns + rows (local netsuite_suiteql) ---
    if isinstance(parsed, dict):
        columns = parsed.get("columns")
        rows = parsed.get("rows")
        if isinstance(columns, list) and isinstance(rows, list):
            row_count = parsed.get("row_count")
            if not isinstance(row_count, int):
                row_count = len(rows)
            query = parsed.get("query")
            if not isinstance(query, str):
                query = params.get("query", params.get("sqlQuery", ""))
            limit = parsed.get("limit")
            if not isinstance(limit, int):
                limit_param = params.get("limit")
                limit = limit_param if isinstance(limit_param, int) else len(rows)
            truncated = bool(parsed.get("truncated") or parsed.get("rows_truncated"))
            rows, row_count, truncated = _cap_stored_rows(rows, row_count, truncated)
            entry: dict[str, Any] = {
                "kind": "table",
                "columns": columns,
                "rows": rows,
                "row_count": row_count,
                "truncated": truncated,
                "query": query,
                "limit": limit,
                "currency_columns": _money_columns(columns),
            }
            # M4: For metric payloads, pass through source_kind so
            # _compute_source_pin_update can distinguish BigQuery vs SuiteQL
            # metrics without mis-pinning NetSuite for a BigQuery metric.
            # Only set when the payload carries the flag (metric trust boundary).
            if parsed.get("suppress_llm_value") is True and "source_kind" in parsed:
                entry["source_kind"] = parsed["source_kind"]
            # Gate B (report provenance): a blessed-metric payload carries
            # definition_version (and source_kind) at the top level — preserve them
            # on the frozen entry so report.compose's metric_headline can attribute
            # the number to its definition version (the §10 audit-citation source).
            # Additive only: never remove keys; copy when present.
            if isinstance(parsed.get("definition_version"), int):
                entry["definition_version"] = parsed["definition_version"]
            if "source_kind" not in entry and "source_kind" in parsed:
                entry["source_kind"] = parsed["source_kind"]
            return entry

    # --- Path 2: reportData (ns_runReport) ---
    if isinstance(parsed, dict):
        report_data = parsed.get("reportData")
        if isinstance(report_data, dict):
            # Shared flatten+cap helper — the SAME one the in-turn intercept uses, so
            # the persisted/sidecar table and the live SSE table can never drift.
            capped = report_data_to_capped_table(report_data)
            if capped is not None:
                columns, rows, line_meta, row_count, truncated = capped
                return {
                    "kind": "table",
                    "columns": columns,
                    "rows": rows,
                    "row_count": row_count,
                    "truncated": truncated,
                    "query": f"ns_runReport(reportId={params.get('reportId', '?')})",
                    "limit": len(rows),
                    # The flattened reportData columns are ["account", "amount"]; tag the
                    # money column(s) so the report renderer accounting-formats ONLY them.
                    "currency_columns": _money_columns(columns),
                    # Per-row statement hierarchy (parallel to rows) for report.compose
                    # (Phase 3) to curate a statement / key-figure callouts. Reaches BOTH
                    # the persisted payload AND the in-turn sidecar (this is that payload).
                    "line_meta": line_meta,
                }
            # reportData present but EMPTY (no report lines). The in-turn intercept's
            # reportData branch also flattens to None here and then yields NO stamped
            # event UNLESS the payload declares success (the financial-items branch
            # stamps a financial_report for success:true). Mirror that gate so the
            # PERSISTED population stays byte-identical to the stamped/sidecar
            # population: only continue to the items/data shapes (Path 3) when
            # success is True; otherwise do NOT persist — a co-present bare items/data
            # list on a failed/empty report would otherwise freeze a result_payload
            # with no matching stamped id, drifting the cross-turn r-id numbering
            # (T2 re-review #1).
            if parsed.get("success") is not True:
                return None

    # --- Path 3: items list-of-dicts (MCP SuiteQL, saved searches) ---
    result = _extract_items_as_table(parsed)
    if result:
        columns, rows = result
        row_count = len(rows)
        query = params.get("sqlQuery", params.get("query", ""))
        rows, row_count, truncated = _cap_stored_rows(rows, row_count, False)
        return {
            "kind": "table",
            "columns": columns,
            "rows": rows,
            "row_count": row_count,
            "truncated": truncated,
            "query": query,
            "limit": len(rows),
            "currency_columns": _money_columns(columns),
        }


def _tool_calls_of(message: Any) -> list[dict[str, Any]]:
    """Normalize a persisted assistant message's tool_calls into a list of dicts.

    Accepts either a plain dict (in-memory turn payload) or a ChatMessage ORM row.
    Tolerates None / non-list shapes.
    """
    if isinstance(message, dict):
        calls = message.get("tool_calls")
    else:
        calls = getattr(message, "tool_calls", None)
    if not isinstance(calls, list):
        return []
    return [c for c in calls if isinstance(c, dict)]


def _message_role(message: Any) -> str | None:
    """Return the message's role (or None when absent). Tolerates dicts and ORM rows."""
    if isinstance(message, dict):
        return message.get("role")
    return getattr(message, "role", None)


def count_payload_bearing_tool_calls(messages: list[Any]) -> int:
    """Count persisted tool_calls carrying a ``result_payload`` dict across the
    given (already-loaded) conversation messages.

    This is the CONVERSATION-ORDINAL counter seed (findings #5/#9/#13): the in-turn
    interceptor numbers THIS turn's data results r(K+1), r(K+2), ... where K is this
    count over the prior conversation history — so the in-turn ids and the persisted
    FALLBACK ids (``resolve_payload_from_messages``, which numbers the SAME population
    1..K) live in ONE id space, never colliding/overwriting across turns.

    Uses the EXACT same criterion as the fallback resolver — a tool_call whose
    ``result_payload`` is a dict — so the two numbering schemes can never drift.

    ROLE FILTER (re-gate r3, finding #4): the cross-turn fallback feeder
    ``load_conversation_tool_messages`` queries ``role == 'assistant'`` ONLY, so the
    seed-K count MUST exclude any non-assistant message carrying a payload-bearing
    tool_call — otherwise the in-turn r(K+1) and the fallback's 1..K numbering would
    drift by one. Messages with NO role (legacy in-memory turn dicts / ORM rows
    missing the attr) are still counted: only an EXPLICIT non-assistant role excludes
    a message, so today's assistant-only write-path is byte-for-byte unaffected.
    """
    count = 0
    for message in messages:
        role = _message_role(message)
        if role is not None and role != "assistant":
            continue
        for call in _tool_calls_of(message):
            if isinstance(call.get("result_payload"), dict):
                count += 1
    return count


def resolve_payload_from_messages(messages: list[Any], result_id: str) -> dict[str, Any]:
    """Resolve a result_id to its FULL, uncapped frozen payload — the CROSS-TURN /
    REGENERATION FALLBACK path (§16.1 fix).

    NOTE (gate cluster A): the PRIMARY same-turn resolution is the eager in-turn
    full-payload sidecar (``result_cache.get_full_payload``), which ``report.compose``
    consults FIRST. This persisted-message walk is only reached on a sidecar miss —
    i.e. a LATER turn (or a report regeneration) composing over results that were
    persisted by an EARLIER turn's assistant ChatMessage. The current turn's
    assistant message is not persisted until AFTER the agent loop, so this path
    cannot (and is not meant to) see THIS turn's just-computed results.

    Walks the assistant messages' ``tool_calls[].result_payload`` (built by
    ``extract_result_payload`` — uncapped, NOT the 50-row Redis result cache) and
    returns the payload whose ``result_id`` matches, or whose positional id
    (``r1``, ``r2``, ...) matches when the call carries no explicit ``result_id``.

    Raises ``KeyError`` when no tool call with a usable ``result_payload`` matches.
    """
    positional = 0
    fallback: dict[str, Any] | None = None
    fallback_key: str | None = None
    for message in messages:
        for call in _tool_calls_of(message):
            payload = call.get("result_payload")
            if not isinstance(payload, dict):
                continue
            positional += 1
            synthetic_id = f"r{positional}"
            explicit_id = call.get("result_id")
            if explicit_id == result_id:
                return payload
            if synthetic_id == result_id:
                # Remember positional match but prefer an explicit-id match if one
                # appears later in the turn (explicit ids win over positional).
                if fallback is None:
                    fallback, fallback_key = payload, synthetic_id
    if fallback is not None and fallback_key == result_id:
        return fallback
    raise KeyError(result_id)


async def load_conversation_tool_messages(db: Any, conversation_id: Any, tenant_id: Any) -> list[Any]:
    """Load the assistant messages (with their full ``tool_calls``) for a session,
    newest turns last.

    ``tenant_id`` is REQUIRED (no default) — this is a defense-in-depth tenant filter.
    The chat tool-execution path does not call ``set_tenant_context``, so RLS may not be
    enforced on this session; we therefore add an explicit
    ``ChatMessage.tenant_id == tenant_id`` predicate rather than relying on RLS alone.
    Keeping the parameter required ensures a future caller cannot silently forget it.
    """
    from sqlalchemy import select

    from app.models.chat import ChatMessage

    if conversation_id is None:
        return []
    rows = (
        (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == conversation_id)
                .where(ChatMessage.tenant_id == tenant_id)
                .where(ChatMessage.role == "assistant")
                .order_by(ChatMessage.created_at, ChatMessage.id)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


def build_tool_call_log_entry(
    *,
    step: int,
    tool_name: str,
    params: dict[str, Any],
    result_str: str,
    duration_ms: int,
    agent_name: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "step": step,
        "tool": tool_name,
        "params": params,
        "result_summary": summarize_tool_result(tool_name, result_str),
        "duration_ms": duration_ms,
    }
    if agent_name:
        entry["agent"] = agent_name

    # UNIFIED SLOT CRITERION (re-gate r3, findings #1/#2): persist result_payload
    # IFF the result is extractable (condition a) AND it is a STAMPED data tool
    # (condition b — is_stamped_data_tool). This is the SAME criterion the in-turn
    # interceptor uses to grant an r-id slot + write the sidecar, so the persisted
    # population (the fallback resolver's denominator) matches the stamped/sidecar
    # population EXACTLY. A payload-bearing but hidden tool (ns_listAllReports →
    # 'other' category, no stamp) is excluded from ALL THREE consumers, so the
    # dense visible-id sequence and the persisted-fallback numbering never drift.
    if is_stamped_data_tool(tool_name):
        result_payload = extract_result_payload(tool_name, params, result_str)
        if result_payload is not None:
            entry["result_payload"] = result_payload

    return entry


def _extract_error_message(parsed: dict[str, Any]) -> str | None:
    error = parsed.get("error")
    if error is True:
        for key in ("message", "detail", "error_message"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return "Query failed"

    if isinstance(error, str) and error.strip():
        return error

    return None


# ---------------------------------------------------------------------------
# Distinct value extraction — prevents LLM from building IN(...) from memory
# ---------------------------------------------------------------------------

import re

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_NUMERIC_RE = re.compile(r"^-?\d+\.?\d*$")
_MAX_DISTINCT = 30  # Skip high-cardinality columns


def extract_distinct_values(result: Any) -> dict[str, list[str]]:
    """Extract distinct string values from categorical columns in a SuiteQL result.

    Returns {column_name: [sorted distinct values]} for columns that are:
    - String-typed (not all numeric, not date-like)
    - Low cardinality (≤ 30 distinct values)
    - Have 2+ distinct values (single-value columns aren't useful)

    Used to inject exact database values into follow-up prompts so the LLM
    doesn't reconstruct value lists from memory (dropping variants).
    """
    if not isinstance(result, dict):
        return {}

    columns = result.get("columns", [])
    rows = result.get("rows", [])

    if not columns or not rows or len(rows) < 2:
        return {}

    distinct: dict[str, set[str]] = {col: set() for col in columns}

    for row in rows:
        if not isinstance(row, (list, tuple)):
            continue
        for i, val in enumerate(row):
            if i < len(columns) and val is not None:
                distinct[columns[i]].add(str(val))

    output: dict[str, list[str]] = {}
    for col, vals in distinct.items():
        if len(vals) < 2 or len(vals) > _MAX_DISTINCT:
            continue
        # Skip numeric columns
        if all(_NUMERIC_RE.match(v) for v in vals):
            continue
        # Skip date columns
        if all(_DATE_RE.match(v) for v in vals):
            continue
        output[col] = sorted(vals)

    return output


def append_distinct_values(result_str: str) -> str:
    """Append _distinct_values to a SuiteQL result JSON string.

    If the result has categorical columns with ≤ 30 distinct values,
    appends them as a _distinct_values key so the LLM can use exact
    values for follow-up CASE WHEN pivots.

    Returns the original string unchanged if no values to add.
    """
    try:
        parsed = json.loads(result_str)
    except (json.JSONDecodeError, TypeError):
        return result_str

    if not isinstance(parsed, dict):
        return result_str

    rows = parsed.get("rows", [])
    if not isinstance(rows, list) or len(rows) < 2:
        return result_str

    values = extract_distinct_values(parsed)
    if not values:
        return result_str

    parsed["_distinct_values"] = values
    return json.dumps(parsed, default=str)
