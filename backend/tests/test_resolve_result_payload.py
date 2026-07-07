# backend/tests/test_resolve_result_payload.py
"""Unit test for resolve_result_payload — the §16.1 fix: a composed report must
resolve the FULL, uncapped frozen payload from ChatMessage.tool_calls[].result_payload,
NOT the 50-row-capped Redis result cache."""

import inspect
import json

import pytest

from app.services.chat.tool_call_results import (
    count_payload_bearing_tool_calls,
    extract_result_payload,
    load_conversation_tool_messages,
    resolve_payload_from_messages,
)

# --- Currency-column tagging for SuiteQL/financial tables (P&L Trend format fix) ---


class TestCurrencyColumnTagging:
    """A SuiteQL (Path 1) result with money + non-money numeric columns tags ONLY the
    money columns by name, so the report renderer accounting-formats `amount` but leaves
    an account-code / period column raw (no over-formatting)."""

    def test_money_columns_helper_tags_by_name_only(self):
        from app.services.chat.tool_call_results import _money_columns

        cols = ["periodname", "startdate", "acctnumber", "acctname", "accttype", "section", "amount"]
        assert _money_columns(cols) == ["amount"]  # acctnumber (a code) is NOT money
        assert _money_columns(["account", "balance"]) == ["balance"]
        assert _money_columns(["year", "order_count", "ratio"]) == []  # not money-named
        # canonical NetSuite/SuiteQL line-amount names — caught by the amount/balance
        # suffix (what an exact allowlist missed), plus compound names:
        assert _money_columns(["netamount", "foreignamount", "stripe_amount", "opening_balance"]) == [
            "netamount",
            "foreignamount",
            "stripe_amount",
            "opening_balance",
        ]
        # OVER-FORMAT GUARD: a non-money column whose name merely CONTAINS a money word
        # (a count/status/code) must NOT be tagged — never show a count as "1,500.00":
        assert _money_columns(["total_count", "creditcount", "creditstatus", "credit_memo_number", "acctnumber"]) == []
        # exact money words still tag (debit/credit/subtotal amounts in a trial balance)
        assert _money_columns(["debit", "credit", "subtotal"]) == ["debit", "credit", "subtotal"]

    def test_suiteql_table_payload_tags_amount_as_currency(self):
        result = {
            "columns": ["periodname", "acctnumber", "amount"],
            "rows": [["Jan 2026", "40001", "1.6442836348665524E7"]],
            "row_count": 1,
            "query": "SELECT ...",
        }
        payload = extract_result_payload("netsuite_suiteql", {}, json.dumps(result))
        assert payload["currency_columns"] == ["amount"]


# --- Re-gate r2 (finding #3): external-MCP top-level "data" key must extract ---


class TestExtractDataKeyShape:
    """The interceptor's data_table path treats ``{"data": [{...}]}`` (the
    documented external-MCP ``ns_runCustomSuiteQL`` shape, chat-orchestration
    rule #3) as a data result and stamps a result_id. ``extract_result_payload``
    MUST recognize the same shape so the sidecar/persisted payload is non-None
    and report.compose can resolve that id — otherwise the single id-assignment
    criterion (payload non-None) would never fire for the most common NetSuite
    data source.
    """

    def test_top_level_data_key_extracts_table(self):
        import json

        mcp_result = {
            "method": "custom_suiteql",
            "queryExecuted": "SELECT t.tranid, t.total FROM transaction t",
            "resultCount": 2,
            "data": [
                {"tranid": "SO-1001", "total": 5000.00},
                {"tranid": "SO-1002", "total": 3200.50},
            ],
        }
        payload = extract_result_payload("ext__abc123def__ns_runcustomsuiteql", {}, json.dumps(mcp_result))
        assert payload is not None, "a top-level 'data' list must extract a payload"
        assert payload["kind"] == "table"
        # Same union-of-keys column derivation as "items".
        assert payload["columns"] == ["tranid", "total"]
        assert payload["rows"] == [["SO-1001", 5000.00], ["SO-1002", 3200.50]]
        assert payload["row_count"] == 2

    def test_data_key_column_union_across_rows(self):
        """Like 'items', columns are the union of keys across all rows (first-seen
        order), so a row missing a later key still aligns."""
        import json

        mcp_result = {
            "data": [
                {"a": 1},
                {"a": 2, "b": 3},
            ]
        }
        payload = extract_result_payload("ext__x__ns_runcustomsuiteql", {}, json.dumps(mcp_result))
        assert payload is not None
        assert payload["columns"] == ["a", "b"]
        assert payload["rows"] == [[1, None], [2, 3]]

    def test_empty_data_key_is_none(self):
        import json

        assert extract_result_payload("ext__x__ns_runcustomsuiteql", {}, json.dumps({"data": []})) is None


class TestExtractFinancialReportShape:
    """Re-gate r3 (finding #5): the financial_report intercept branch STAMPS an id
    on every successful result (even empty-period), so extract_result_payload MUST
    return a non-None payload for the financial shape — otherwise the stamped id
    dangles (resolves to nothing in report.compose)."""

    def test_financial_report_with_items_extracts_table(self):
        import json

        financial = {
            "success": True,
            "report_type": "income_statement",
            "period": "Feb 2026",
            "columns": ["account", "amount"],
            "items": [
                {"account": "Revenue", "amount": 100000},
                {"account": "Net Income", "amount": 60000},
            ],
            "total_rows": 2,
            "summary": {"total_revenue": 100000, "net_income": 60000},
        }
        payload = extract_result_payload("netsuite_financial_report", {}, json.dumps(financial))
        assert payload is not None, "a successful financial report must extract a payload"
        assert payload["kind"] == "table"
        assert payload["row_count"] == 2
        assert payload["rows"] == [["Revenue", 100000], ["Net Income", 60000]]

    def test_empty_financial_report_extracts_empty_table(self):
        """A zero-activity-period financial report (success, items=[]) must STILL
        extract a non-None (empty-rows) payload — the stamped id must resolve."""
        import json

        empty = {
            "success": True,
            "report_type": "income_statement",
            "period": "Jun 2026",
            "columns": ["account", "amount"],
            "items": [],
            "total_rows": 0,
            "summary": {"total_revenue": 0, "net_income": 0},
        }
        payload = extract_result_payload("netsuite_financial_report", {}, json.dumps(empty))
        assert payload is not None, "an empty-but-successful financial report must NOT extract None"
        assert payload["kind"] == "table"
        assert payload["row_count"] == 0
        assert payload["rows"] == []

    def test_failed_financial_report_is_none(self):
        import json

        failed = {"success": False, "error": "Query failed", "report_type": "x", "period": "y"}
        assert extract_result_payload("netsuite_financial_report", {}, json.dumps(failed)) is None


class TestStoredPayloadRowCap:
    """Re-gate r3 (finding #6, CONFIRMED): the persisted ChatMessage.tool_calls[]
    .result_payload (and the in-turn sidecar) must be capped at MAX_STORED_PAYLOAD_ROWS
    (2000) so an ordinary broad SuiteQL turn (up to 50k rows) doesn't bake multi-MB JSONB
    into Postgres. The TRUE row_count is preserved and truncated=True is set. This is the
    STORAGE cap; report rendering curates much further (first _REPORT_TABLE_TOP_K rows per
    table, charts at 100)."""

    def test_extract_caps_rows_at_2000_preserves_true_count(self):
        import json

        from app.services.chat.tool_call_results import MAX_STORED_PAYLOAD_ROWS

        assert MAX_STORED_PAYLOAD_ROWS == 2000
        rows = [[f"SO-{i:05d}", i * 1.5] for i in range(2500)]
        result = {
            "columns": ["tranid", "amount"],
            "rows": rows,
            "row_count": 2500,
            "query": "SELECT tranid, amount FROM transaction",
        }
        payload = extract_result_payload("netsuite_suiteql", {}, json.dumps(result, default=str))
        assert payload is not None
        assert len(payload["rows"]) == MAX_STORED_PAYLOAD_ROWS, "stored rows must be capped at 2000"
        assert payload["row_count"] == 2500, "the TRUE pre-cap row_count must be preserved"
        assert payload["truncated"] is True, "a capped payload must be marked truncated"

    def test_extract_under_cap_is_unchanged(self):
        import json

        rows = [[f"SO-{i:04d}", i] for i in range(600)]
        result = {"columns": ["tranid", "amount"], "rows": rows, "row_count": 600, "query": "q"}
        payload = extract_result_payload("netsuite_suiteql", {}, json.dumps(result, default=str))
        assert payload is not None
        assert len(payload["rows"]) == 600
        assert payload["row_count"] == 600
        assert payload["truncated"] is False

    def test_items_shape_caps_at_2000(self):
        """The list-of-dicts (external-MCP 'data'/'items') path must cap too."""
        import json

        from app.services.chat.tool_call_results import MAX_STORED_PAYLOAD_ROWS

        items = [{"tranid": f"SO-{i:05d}", "total": i} for i in range(2500)]
        payload = extract_result_payload("ext__x__ns_runcustomsuiteql", {}, json.dumps({"data": items}))
        assert payload is not None
        assert len(payload["rows"]) == MAX_STORED_PAYLOAD_ROWS
        assert payload["row_count"] == 2500
        assert payload["truncated"] is True


def _msg(tool_calls):
    return {"role": "assistant", "tool_calls": tool_calls}


def test_resolve_returns_full_uncapped_payload():
    """A source result with >50 rows must come back uncapped."""
    big_rows = [["P%d" % i, str(i)] for i in range(120)]
    messages = [
        _msg(
            [
                {
                    "tool": "netsuite.suiteql",
                    "result_id": "r1",
                    "result_payload": {
                        "kind": "table",
                        "columns": ["Period", "N"],
                        "rows": big_rows,
                        "row_count": 120,
                    },
                }
            ]
        )
    ]
    payload = resolve_payload_from_messages(messages, "r1")
    assert payload["row_count"] == 120
    assert len(payload["rows"]) == 120  # NOT capped at 50


def test_resolve_unknown_id_raises_keyerror():
    messages = [_msg([{"tool": "x", "result_id": "r1", "result_payload": {"rows": []}}])]
    try:
        resolve_payload_from_messages(messages, "missing")
    except KeyError:
        return
    raise AssertionError("expected KeyError for unknown result_id")


def test_resolve_matches_synthetic_index_id():
    """Tool calls without an explicit result_id are addressable by positional id (r1, r2...)."""
    messages = [
        _msg(
            [
                {"tool": "a", "result_payload": {"rows": [["x"]], "row_count": 1}},
                {"tool": "b", "result_payload": {"rows": [["y"]], "row_count": 1}},
            ]
        )
    ]
    assert resolve_payload_from_messages(messages, "r2")["rows"] == [["y"]]


# --- re-gate r2 (findings #5/#9/#13): conversation-ordinal id space ---


class TestConversationOrdinalCount:
    """``count_payload_bearing_tool_calls`` is the counter-seed for the in-turn
    interceptor: it counts persisted tool_calls carrying a ``result_payload`` dict
    using the EXACT same criterion the fallback resolver numbers them by (1..K). The
    next in-turn result therefore gets r(K+1) — one id space across turns."""

    def test_counts_only_payload_bearing_calls(self):
        messages = [
            _msg(
                [
                    {"tool": "rag_search"},  # no result_payload — not counted
                    {"tool": "a", "result_payload": {"rows": [["x"]], "row_count": 1}},
                ]
            ),
            _msg(
                [
                    {"tool": "b", "result_payload": {"rows": [["y"]], "row_count": 1}},
                    {"tool": "workspace.list", "result_payload": "not-a-dict"},  # not a dict
                ]
            ),
        ]
        assert count_payload_bearing_tool_calls(messages) == 2

    def test_empty_history_is_zero(self):
        assert count_payload_bearing_tool_calls([]) == 0
        assert count_payload_bearing_tool_calls([_msg([])]) == 0

    def test_counts_only_assistant_role_messages(self):
        """Re-gate r3 (finding #4): the seed-K count must apply the SAME role filter
        as the cross-turn fallback feeder (load_conversation_tool_messages, which
        queries role == 'assistant'). A non-assistant message carrying a
        result_payload-bearing tool_calls entry must NOT be counted — otherwise the
        in-turn r(K+1) and the persisted-fallback 1..K numbering drift by one."""
        messages = [
            {
                "role": "assistant",
                "tool_calls": [{"tool": "a", "result_payload": {"rows": [["x"]], "row_count": 1}}],
            },
            {
                # A non-assistant row carrying a payload-bearing tool_call: the
                # fallback (assistant-only query) never sees it, so the seed must not either.
                "role": "tool",
                "tool_calls": [{"tool": "b", "result_payload": {"rows": [["y"]], "row_count": 1}}],
            },
            {
                "role": "user",
                "tool_calls": [{"tool": "c", "result_payload": {"rows": [["z"]], "row_count": 1}}],
            },
        ]
        assert count_payload_bearing_tool_calls(messages) == 1, (
            "only assistant-role messages may be counted (mirror the fallback resolver)"
        )

    def test_message_without_role_attr_still_counts(self):
        """Dict messages with no 'role' key (legacy in-memory turn payloads) and ORM
        rows missing the attr must default to counting — only an EXPLICIT non-assistant
        role excludes a message, so today's assistant-only write-path is unaffected."""

        class _OrmRowNoRole:
            tool_calls = [{"tool": "a", "result_payload": {"rows": [["x"]], "row_count": 1}}]

        # dict with no role key
        no_role_dict = {"tool_calls": [{"tool": "b", "result_payload": {"rows": [["y"]], "row_count": 1}}]}
        assert count_payload_bearing_tool_calls([no_role_dict, _OrmRowNoRole()]) == 2


def test_inturn_result_aligns_with_fallback_across_turns():
    """The core invariant the fix establishes: an in-turn id (r4) and the persisted
    fallback id (r2 from turn 1) live in ONE conversation-ordinal id space.

    Seed 2 persisted turns producing 3 payload-bearing calls (r1, r2, r3). The next
    in-turn result, seeded with start_count = K (= 3 here), gets r4. The fallback
    resolves r2 to turn 1's SECOND payload, and r4 to the new in-turn result — same
    id space, no collision/overwrite.
    """
    from app.services.chat.orchestrator import _make_tool_interceptor

    # 2 persisted turns: turn 1 has two payload-bearing calls (r1, r2), turn 2 one (r3).
    messages = [
        _msg(
            [
                {"tool": "t1a", "result_payload": {"rows": [["t1a"]], "row_count": 1}},
                {"tool": "t1b", "result_payload": {"rows": [["t1b"]], "row_count": 1}},
            ]
        ),
        _msg([{"tool": "t2a", "result_payload": {"rows": [["t2a"]], "row_count": 1}}]),
    ]

    # K = number of payload-bearing results already in history.
    k = count_payload_bearing_tool_calls(messages)
    assert k == 3

    # Fallback resolves the persisted ids in conversation order.
    assert resolve_payload_from_messages(messages, "r1")["rows"] == [["t1a"]]
    assert resolve_payload_from_messages(messages, "r2")["rows"] == [["t1b"]]
    assert resolve_payload_from_messages(messages, "r3")["rows"] == [["t2a"]]

    # A NEW in-turn result, seeded with start_count=K, is r(K+1) = r4 — the SAME
    # id space, distinct from every persisted id.
    captured: dict = {}

    def _cb(tool_name, event_type_str, event_data, result_id=None, params=None, result_str=None, full_payload=None):
        captured["result_id"] = result_id

    interceptor = _make_tool_interceptor(cache_callback=_cb, start_count=k)
    new_payload = {"columns": ["c"], "rows": [["inturn"]], "row_count": 1, "query": "q"}
    import json

    _, llm_str = interceptor("netsuite_suiteql", json.dumps(new_payload))
    assert json.loads(llm_str)["result_id"] == "r4"
    assert captured["result_id"] == "r4"


# --- Slice A (live-dashboard reports): the sidecar write carries {tool, params} ---
# _on_tool_intercepted receives the executed tool_name + params and used to discard
# them; the extracted module-level helper now forwards both into the sidecar envelope
# so a same-turn report.compose can capture the refresh recipe's meta.


def test_sidecar_write_helper_carries_tool_and_params():
    from unittest.mock import patch

    from app.services.chat import orchestrator

    calls: dict = {}

    def fake_cache(conversation_id, result_id, payload, *, tool_name=None, params=None):
        calls.update(
            conversation_id=conversation_id,
            result_id=result_id,
            payload=payload,
            tool_name=tool_name,
            params=params,
        )

    with patch("app.services.chat.result_cache.cache_full_payload", fake_cache):
        orchestrator._write_full_payload_sidecar(
            "conv-9", "r4", "netsuite_suiteql", {"query": "SELECT 1"}, {"rows": [[1]], "row_count": 1}
        )
    assert calls == {
        "conversation_id": "conv-9",
        "result_id": "r4",
        "payload": {"rows": [[1]], "row_count": 1},
        "tool_name": "netsuite_suiteql",
        "params": {"query": "SELECT 1"},
    }


def test_sidecar_write_helper_is_best_effort_never_raises():
    from unittest.mock import patch

    from app.services.chat import orchestrator

    def boom(*a, **k):
        raise RuntimeError("redis down")

    with patch("app.services.chat.result_cache.cache_full_payload", boom):
        orchestrator._write_full_payload_sidecar("c", "r1", "t", {}, {"rows": []})  # must not raise


def test_interceptor_callback_receives_tool_and_params():
    """Pin the plumbing recipe capture rides on: _make_tool_interceptor threads the
    executed tool_name + params (+ the precomputed full_payload) to the callback."""
    import json

    from app.services.chat.orchestrator import _make_tool_interceptor

    captured: dict = {}

    def _cb(tool_name, event_type_str, event_data, result_id=None, params=None, result_str=None, full_payload=None):
        captured.update(tool_name=tool_name, params=params, result_id=result_id, full_payload=full_payload)

    interceptor = _make_tool_interceptor(cache_callback=_cb)
    payload = {"columns": ["c"], "rows": [["x"]], "row_count": 1, "query": "SELECT 1"}
    interceptor("netsuite_suiteql", json.dumps(payload), params={"query": "SELECT 1"})
    assert captured["tool_name"] == "netsuite_suiteql"
    assert captured["params"] == {"query": "SELECT 1"}
    assert captured["result_id"] == "r1"
    assert captured["full_payload"] is not None


# --- Slice A: the cross-turn meta walker mirrors the payload resolver EXACTLY ---
# collect_tool_meta_from_messages recovers {tool, params} per rid from persisted
# tool_calls[] — same population criterion + same counter as
# resolve_payload_from_messages, so meta availability tracks payload availability
# (the anti-drift lock is asserting both walks agree on every rid).


def test_meta_map_positional_ids_match_payload_resolver():
    from app.services.chat.tool_call_results import collect_tool_meta_from_messages

    messages = [
        _msg(
            [
                {
                    "tool": "netsuite_suiteql",
                    "params": {"query": "SELECT a"},
                    "result_payload": {"rows": [["t1a"]], "row_count": 1},
                },
                {"tool": "no_payload_tool", "params": {"x": 1}},  # skipped by BOTH walks
                {
                    "tool": "ext__0f3c9a2e00000000000000000000beef__ns_runReport",
                    "params": {"reportId": 7},
                    "result_payload": {"rows": [["t1b"]], "row_count": 1},
                },
            ]
        ),
        _msg(
            [
                {
                    "tool": "metric_compute",
                    "params": {"metric_id": "m"},
                    "result_payload": {"rows": [["t2a"]], "row_count": 1},
                }
            ]
        ),
    ]
    meta = collect_tool_meta_from_messages(messages)
    assert set(meta) == {"r1", "r2", "r3"}
    # every rid's meta corresponds to the SAME entry the payload resolver returns
    assert meta["r1"]["tool"] == "netsuite_suiteql"
    assert resolve_payload_from_messages(messages, "r1")["rows"] == [["t1a"]]
    assert meta["r2"]["tool"].startswith("ext__")
    assert resolve_payload_from_messages(messages, "r2")["rows"] == [["t1b"]]
    assert meta["r3"]["params"] == {"metric_id": "m"}
    assert resolve_payload_from_messages(messages, "r3")["rows"] == [["t2a"]]


def test_meta_map_explicit_result_id_wins_like_the_resolver():
    """No production writer sets an explicit result_id key today, but the resolver
    supports it — the meta walker must register the explicit key too (in ADDITION to
    the positional slot the entry still consumes)."""
    from app.services.chat.tool_call_results import collect_tool_meta_from_messages

    messages = [
        _msg([{"tool": "t_expl", "params": {}, "result_id": "rX", "result_payload": {"rows": [["e"]], "row_count": 1}}])
    ]
    meta = collect_tool_meta_from_messages(messages)
    assert meta["rX"]["tool"] == "t_expl"
    assert resolve_payload_from_messages(messages, "rX")["rows"] == [["e"]]
    assert meta["r1"]["tool"] == "t_expl"  # the resolver's positional fallback reaches it too


def test_meta_map_skips_metaless_entries_without_shifting_numbering():
    """A payload-bearing call missing tool/params still CONSUMES its positional slot
    (the counter must stay aligned with the resolver) but yields no meta — the recipe
    builder then fails closed for that rid."""
    from app.services.chat.tool_call_results import collect_tool_meta_from_messages

    messages = [
        _msg(
            [
                {"result_payload": {"rows": [["x"]], "row_count": 1}},  # no tool/params
                {
                    "tool": "netsuite_suiteql",
                    "params": {"query": "q"},
                    "result_payload": {"rows": [["y"]], "row_count": 1},
                },
            ]
        )
    ]
    meta = collect_tool_meta_from_messages(messages)
    assert "r1" not in meta  # meta unrecoverable
    assert meta["r2"]["tool"] == "netsuite_suiteql"  # numbering NOT shifted
    assert resolve_payload_from_messages(messages, "r2")["rows"] == [["y"]]


# --- Gate D (finding #18): defense-in-depth tenant filter on the resolver query ---


class _FakeResult:
    def scalars(self):
        return self

    def all(self):
        return []


class _CapturingDB:
    """Captures the SQLAlchemy statement passed to execute() so the test can assert
    the WHERE clause includes a tenant_id predicate."""

    def __init__(self):
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _FakeResult()


def test_load_conversation_tool_messages_requires_tenant_id():
    """tenant_id must be a REQUIRED parameter (no default) so callers can't forget it."""
    sig = inspect.signature(load_conversation_tool_messages)
    assert "tenant_id" in sig.parameters, "load_conversation_tool_messages must accept tenant_id"
    assert sig.parameters["tenant_id"].default is inspect.Parameter.empty, (
        "tenant_id must be REQUIRED (no default) — defense-in-depth against RLS not being set"
    )


@pytest.mark.asyncio
async def test_load_conversation_tool_messages_filters_by_tenant():
    """The resolver SELECT must carry an explicit ChatMessage.tenant_id == tenant_id
    predicate (RLS may not be set on the chat tool-exec path)."""
    import uuid

    db = _CapturingDB()
    tenant_uuid = uuid.UUID("11111111-1111-1111-1111-111111111111")
    conv_uuid = uuid.UUID("22222222-2222-2222-2222-222222222222")
    await load_conversation_tool_messages(db, conv_uuid, tenant_id=tenant_uuid)
    assert db.statements, "expected a SELECT to be executed"
    compiled = str(db.statements[0].compile(compile_kwargs={"literal_binds": True}))
    # The query must constrain BOTH session_id AND tenant_id (defense-in-depth).
    assert "tenant_id = " in compiled, f"tenant_id filter missing from query:\n{compiled}"
    # UUID literals render dashless; match on the hex form.
    assert tenant_uuid.hex in compiled, f"tenant_id value not bound into query:\n{compiled}"


@pytest.mark.asyncio
async def test_load_conversation_tool_messages_none_conversation_returns_empty():
    db = _CapturingDB()
    out = await load_conversation_tool_messages(db, None, tenant_id="ten-1")
    assert out == []
    assert not db.statements  # short-circuits before any SELECT
