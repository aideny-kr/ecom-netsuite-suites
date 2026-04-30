"""Tests for the history tool-trace renderer.

Context: Olivia's 2026-04-09 session showed the agent solving a
transactionShippingAddress join in Turn 2, then on Turn 3 completely forgetting
the working pattern because the orchestrator's history loader only replays
message `content` — never the `tool_calls` column from the DB. The renderer
tested here produces a compact, LLM-friendly trace of the previous turn's
tool calls (final query + pass/fail + key discoveries) so the next turn can
reuse proven patterns instead of rediscovering them from scratch.
"""

from app.services.chat.history_tool_trace import build_history_dicts, render_tool_trace


class TestRenderToolTrace:
    def test_empty_list_returns_empty_string(self):
        assert render_tool_trace([]) == ""

    def test_none_returns_empty_string(self):
        assert render_tool_trace(None) == ""

    def test_single_successful_suiteql(self):
        calls = [
            {
                "step": 0,
                "tool": "netsuite_suiteql",
                "params": {"query": "SELECT id FROM customer WHERE id = 1"},
                "result_summary": "Returned 1 row",
                "duration_ms": 500,
            }
        ]
        out = render_tool_trace(calls)
        assert "<tool_trace" in out
        assert "netsuite_suiteql" in out
        assert "OK" in out or "1 row" in out
        assert "SELECT id FROM customer" in out

    def test_single_failed_suiteql_includes_error_reason(self):
        """Failures should be one-liners with the error reason, not the full SQL.

        This is the Olivia Turn 1 case — the agent kept retrying the same
        NOT_EXPOSED error. The trace must make failures obvious so the next
        turn doesn't repeat them.
        """
        calls = [
            {
                "step": 0,
                "tool": "netsuite_suiteql",
                "params": {"query": "SELECT t.shipcountry FROM transaction t"},
                "result_summary": (
                    "NetSuite query failed: NetSuite API error 400: "
                    "{\"detail\":\"Field 'shipcountry' for record 'transaction' "
                    "was not found. Reason: NOT_EXPOSED"
                ),
                "duration_ms": 1000,
            }
        ]
        out = render_tool_trace(calls)
        assert "FAILED" in out or "ERROR" in out
        assert "shipcountry" in out
        assert "NOT_EXPOSED" in out

    def test_external_mcp_suiteql_uses_sqlquery_param(self):
        """External MCP tools pass the query in `sqlQuery`, not `query`."""
        calls = [
            {
                "step": 0,
                "tool": "ext__abc123__ns_runCustomSuiteQL",
                "params": {"sqlQuery": "SELECT id FROM item FETCH FIRST 5 ROWS ONLY"},
                "result_summary": "Returned 5 rows",
                "duration_ms": 800,
            }
        ]
        out = render_tool_trace(calls)
        assert "ns_runCustomSuiteQL" in out
        assert "SELECT id FROM item" in out
        # External MCP prefix should be stripped for readability
        assert "ext__abc123__" not in out

    def test_metadata_discovery_surfaces_tool_target(self):
        """ns_getSuiteQLMetadata calls should show which recordType was inspected —
        this is how the agent discovers field names like nKey."""
        calls = [
            {
                "step": 0,
                "tool": "ext__abc__ns_getSuiteQLMetadata",
                "params": {"recordType": "transactionShippingAddress"},
                "result_summary": '{"success": true, "metadata": {"type": "object", "properties": {"nKey": {...}}}}',
                "duration_ms": 1900,
            }
        ]
        out = render_tool_trace(calls)
        assert "ns_getSuiteQLMetadata" in out
        assert "transactionShippingAddress" in out

    def test_truncates_long_sql(self):
        """Long SQL (> 400 chars) should be truncated with an ellipsis so the
        trace doesn't blow up the history budget."""
        long_sql = "SELECT " + ", ".join(f"col_{i}" for i in range(200)) + " FROM transaction"
        calls = [
            {
                "step": 0,
                "tool": "netsuite_suiteql",
                "params": {"query": long_sql},
                "result_summary": "Returned 10 rows",
                "duration_ms": 1000,
            }
        ]
        out = render_tool_trace(calls)
        # Trace line length cap somewhere around 400-500 chars
        longest_line = max(len(line) for line in out.split("\n"))
        assert longest_line < 600
        assert "…" in out or "..." in out

    def test_olivia_turn_2_trace_surfaces_nkey_join(self):
        """Regression: Olivia's Turn 2 was the successful run. The trace must
        surface the critical `transactionShippingAddress sa ON sa.nKey = t.shippingAddress`
        pattern so Turn 3 can reuse it instead of reverting to `t.shipcountry`.
        """
        calls = [
            {
                "step": 0,
                "tool": "netsuite_suiteql",
                "params": {"query": "SELECT t.id, t.tranid, t.shipcountry FROM transaction t"},
                "result_summary": "NetSuite query failed: Field 'shipcountry' NOT_EXPOSED",
                "duration_ms": 758,
            },
            {
                "step": 1,
                "tool": "ext__fc1cba33__ns_getSuiteQLMetadata",
                "params": {"recordType": "transactionShippingAddress"},
                "result_summary": '{"success": true, "metadata": {"properties": {"nKey": {...}, "country": {...}}}}',
                "duration_ms": 1913,
            },
            {
                "step": 2,
                "tool": "ext__fc1cba33__ns_runCustomSuiteQL",
                "params": {
                    "sqlQuery": (
                        "SELECT BUILTIN.DF(sa.country) AS ship_country, "
                        "COUNT(DISTINCT t.id) AS total_orders "
                        "FROM transaction t "
                        "JOIN transactionShippingAddress sa ON sa.nKey = t.shippingAddress "
                        "WHERE t.type = 'SalesOrd'"
                    ),
                },
                "result_summary": "Returned 4 rows",
                "duration_ms": 2715,
            },
        ]
        out = render_tool_trace(calls)
        # Failed t.shipcountry attempt should show up as a failure
        assert "NOT_EXPOSED" in out
        # Metadata discovery of the right table
        assert "transactionShippingAddress" in out
        # The successful join pattern is the whole point
        assert "sa.nKey = t.shippingAddress" in out
        assert "ns_runCustomSuiteQL" in out

    def test_non_sql_tools_render_params_compactly(self):
        """Non-SuiteQL tools (ns_getRecord, ns_runReport, etc.) should still
        appear in the trace with their key params."""
        calls = [
            {
                "step": 0,
                "tool": "ext__abc__ns_runReport",
                "params": {"reportId": "123", "filters": {"period": "Q1 2026"}},
                "result_summary": "Returned 45 rows",
                "duration_ms": 4000,
            }
        ]
        out = render_tool_trace(calls)
        assert "ns_runReport" in out
        assert "123" in out or "Q1 2026" in out

    def test_wraps_in_single_block(self):
        """Output should be wrapped in a single <tool_trace>...</tool_trace>
        block so the LLM sees it as a coherent unit."""
        calls = [
            {
                "step": 0,
                "tool": "netsuite_suiteql",
                "params": {"query": "SELECT 1"},
                "result_summary": "Returned 1 row",
                "duration_ms": 10,
            }
        ]
        out = render_tool_trace(calls)
        assert out.count("<tool_trace") == 1
        assert out.count("</tool_trace>") == 1


class TestBuildHistoryDicts:
    """Integration tests for the orchestrator's history loader — verifies
    that tool_calls metadata is replayed to the LLM on follow-up turns."""

    def _make_olivia_session(self) -> list[dict]:
        """Reproduce the first 5 messages of Olivia's 2026-04-09 session:

        0 user: give me sales data for NO/CH/NZ/SG
        1 assistant: source picker (empty content)
        2 assistant: Turn 1 — failed, gave up (with big tool_calls log)
        3 user: using shipping country
        4 assistant: Turn 2 — SUCCESS via transactionShippingAddress join
        """
        return [
            {
                "role": "user",
                "content": "give me the sales data of Norway, Switzerland, NZ, Singapore today",
                "content_summary": None,
                "tool_calls": None,
            },
            {
                "role": "assistant",
                "content": "",
                "content_summary": None,
                "tool_calls": None,
            },
            {
                "role": "assistant",
                "content": "I hit a technical wall with this one...",
                "content_summary": "User asked for country sales; query failed with NOT_EXPOSED",
                "tool_calls": [
                    {
                        "step": 0,
                        "tool": "netsuite_suiteql",
                        "params": {"query": "SELECT t.shipcountry FROM transaction t"},
                        "result_summary": "NetSuite query failed: Field 'shipcountry' NOT_EXPOSED",
                        "duration_ms": 1000,
                    }
                ],
            },
            {
                "role": "user",
                "content": "using shipping country to identify orders",
                "content_summary": None,
                "tool_calls": None,
            },
            {
                "role": "assistant",
                "content": "Here's today's sales data for the four countries: Switzerland 16 orders...",
                "content_summary": "Provided NO/CH/NZ/SG sales via shipping country join",
                "tool_calls": [
                    {
                        "step": 0,
                        "tool": "ext__fc1cba33__ns_runCustomSuiteQL",
                        "params": {
                            "sqlQuery": (
                                "SELECT BUILTIN.DF(sa.country), COUNT(DISTINCT t.id) "
                                "FROM transaction t "
                                "JOIN transactionShippingAddress sa ON sa.nKey = t.shippingAddress "
                                "WHERE t.type = 'SalesOrd'"
                            ),
                        },
                        "result_summary": "Returned 4 rows",
                        "duration_ms": 2715,
                    }
                ],
            },
        ]

    def test_replays_tool_trace_for_recent_assistant_messages(self):
        """The kept-recent assistant message with tool_calls should have
        its trace appended to the content sent to the LLM."""
        messages = self._make_olivia_session()
        history, summarised = build_history_dicts(messages, keep_recent=4)

        # The Turn-2 success (index 4) is the most recent assistant message.
        # Its content should contain BOTH the prose AND the tool_trace block.
        turn2_content = history[-1]["content"]
        assert "Switzerland 16 orders" in turn2_content
        assert "<tool_trace from previous turn>" in turn2_content
        # The critical join pattern must survive into the next turn's context
        assert "sa.nKey = t.shippingAddress" in turn2_content
        assert "transactionShippingAddress" in turn2_content

    def test_replays_failure_traces_so_agent_avoids_repeats(self):
        """Turn 1's failure trace (NOT_EXPOSED) should survive into the next
        turn so the agent doesn't retry the exact same broken query.
        Olivia's real session did this 5+ times."""
        messages = self._make_olivia_session()
        history, _ = build_history_dicts(messages, keep_recent=4)

        # Turn 1 (index 2) is inside the kept-recent window (last 4 messages).
        # Find the turn 1 message in history — it's the one with "technical wall"
        turn1 = next(h for h in history if "technical wall" in h.get("content", ""))
        assert "<tool_trace" in turn1["content"]
        assert "NOT_EXPOSED" in turn1["content"]
        assert "shipcountry" in turn1["content"]

    def test_no_trace_for_old_messages_using_content_summary(self):
        """Older assistant messages beyond keep_recent should use content_summary,
        and the tool trace is NOT injected for summarised messages (trace is
        only meaningful for recent, verbatim content)."""
        # Build a 10-message history; keep_recent=4 means msgs 0-5 get summary path
        messages = []
        for i in range(10):
            role = "user" if i % 2 == 0 else "assistant"
            messages.append(
                {
                    "role": role,
                    "content": f"verbatim content {i}",
                    "content_summary": f"summary {i}" if role == "assistant" else None,
                    "tool_calls": (
                        [
                            {
                                "step": 0,
                                "tool": "netsuite_suiteql",
                                "params": {"query": f"SELECT {i}"},
                                "result_summary": "Returned 1 row",
                                "duration_ms": 10,
                            }
                        ]
                        if role == "assistant"
                        else None
                    ),
                }
            )

        history, summarised = build_history_dicts(messages, keep_recent=4)

        # Old assistant messages (indices 1, 3, 5) should use content_summary,
        # not include <tool_trace>
        old_assistants = [h for h in history[:6] if h["role"] == "assistant"]
        for old in old_assistants:
            assert "<tool_trace" not in old["content"]

        # Recent assistant messages (indices 7, 9) ARE in keep_recent window
        # and should have traces
        recent_assistants = [h for h in history[-4:] if h["role"] == "assistant"]
        assert all("<tool_trace" in h["content"] for h in recent_assistants)

    def test_empty_message_list(self):
        history, summarised = build_history_dicts([], keep_recent=4)
        assert history == []
        assert summarised == 0

    def test_user_messages_never_get_trace(self):
        """Only assistant messages can have tool_calls — user messages are
        untouched."""
        messages = [
            {
                "role": "user",
                "content": "What's the weather?",
                "content_summary": None,
                "tool_calls": [{"step": 0, "tool": "x", "params": {}, "result_summary": "ok"}],
            }
        ]
        history, _ = build_history_dicts(messages, keep_recent=4)
        assert "<tool_trace" not in history[0]["content"]
        assert history[0]["content"] == "What's the weather?"

    def test_include_tool_trace_false_disables_feature(self):
        """The feature can be disabled (for backwards compat or debugging)."""
        messages = self._make_olivia_session()
        history, _ = build_history_dicts(messages, keep_recent=4, include_tool_trace=False)
        for h in history:
            assert "<tool_trace" not in h.get("content", "")


class TestClarificationHistorySurfacing:
    """Codex round 10 P2 Bug 1: clarification turns persist with content="" so
    chat history serialization passes the LLM an empty assistant message —
    the agent has no way to look up what option B's definition was when the
    resume directive says "Picked option B (source: netsuite)".

    Fix: when an assistant message has empty content AND structured_output
    of type "clarification", synthesize a compact options summary as the
    message content so the next turn's LLM can interpret the directive's
    option ID.
    """

    @staticmethod
    def _clarification_msg(*, options, summary="Revenue can mean two things."):
        return {
            "role": "assistant",
            "content": "",
            "content_summary": None,
            "tool_calls": None,
            "structured_output": {
                "type": "clarification",
                "status": "pending",
                "options": options,
                "default_id": "A",
                "ambiguity_summary": summary,
                "confirmation_token": "deadbeef" * 8,
                "expires_at": "2099-01-01T00:00:00Z",
            },
        }

    def test_clarification_history_surfaces_options(self):
        options = [
            {
                "id": "A",
                "title": "NetSuite GL revenue",
                "rationale": "GL recognized revenue",
                "source": "netsuite",
                "is_default": True,
            },
            {
                "id": "B",
                "title": "BigQuery checkout totals",
                "rationale": "ecommerce gross totals",
                "source": "bigquery",
                "is_default": False,
            },
        ]
        messages = [
            {"role": "user", "content": "What's our revenue?", "content_summary": None, "tool_calls": None},
            self._clarification_msg(options=options),
            {"role": "user", "content": "Option B", "content_summary": None, "tool_calls": None},
        ]
        history, _ = build_history_dicts(messages, keep_recent=4)

        # Find the (formerly empty) clarification assistant message
        assistant_msgs = [h for h in history if h["role"] == "assistant"]
        assert len(assistant_msgs) == 1
        clarif_content = assistant_msgs[0]["content"]

        # Must surface BOTH options' titles + sources so the agent can
        # disambiguate the next turn's "Picked option B" directive.
        assert "Option A" in clarif_content
        assert "Option B" in clarif_content
        assert "NetSuite GL revenue" in clarif_content
        assert "BigQuery checkout totals" in clarif_content
        assert "netsuite" in clarif_content
        assert "bigquery" in clarif_content

    def test_clarification_history_no_structured_output_no_surfacing(self):
        """An empty assistant message without structured_output must NOT
        be synthesized — that would invent content."""
        messages = [
            {"role": "user", "content": "hi", "content_summary": None, "tool_calls": None},
            {
                "role": "assistant",
                "content": "",
                "content_summary": None,
                "tool_calls": None,
                "structured_output": None,
            },
        ]
        history, _ = build_history_dicts(messages, keep_recent=4)
        assistant = next(h for h in history if h["role"] == "assistant")
        assert assistant["content"] == ""

    def test_clarification_history_non_clarification_type_no_surfacing(self):
        """Other structured_output types (e.g. data_table) must NOT trigger
        the synthesis — only ``type == "clarification"``."""
        messages = [
            {"role": "user", "content": "hi", "content_summary": None, "tool_calls": None},
            {
                "role": "assistant",
                "content": "",
                "content_summary": None,
                "tool_calls": None,
                "structured_output": {"type": "data_table", "data": {}},
            },
        ]
        history, _ = build_history_dicts(messages, keep_recent=4)
        assistant = next(h for h in history if h["role"] == "assistant")
        # No "Option A" injected
        assert "Option A" not in assistant["content"]

    def test_clarification_history_truncates_long_titles(self):
        """Bound size: titles are truncated to 200 chars per field."""
        long_title = "x" * 500
        long_rationale = "y" * 500
        options = [
            {
                "id": "A",
                "title": long_title,
                "rationale": long_rationale,
                "source": "netsuite",
                "is_default": True,
            },
            {
                "id": "B",
                "title": "Short",
                "rationale": "Short",
                "source": "bigquery",
                "is_default": False,
            },
        ]
        messages = [
            {"role": "user", "content": "q", "content_summary": None, "tool_calls": None},
            self._clarification_msg(options=options),
        ]
        history, _ = build_history_dicts(messages, keep_recent=4)
        clarif = next(h for h in history if h["role"] == "assistant")
        # Long title appears truncated, not 500 chars verbatim
        assert long_title not in clarif["content"]
        assert "x" * 200 in clarif["content"]  # at least a 200-char run survived

    def test_clarification_history_existing_content_unchanged(self):
        """If the assistant message already has non-empty content, do not
        overwrite it with the synthesized summary."""
        msg = self._clarification_msg(
            options=[
                {
                    "id": "A",
                    "title": "X",
                    "rationale": "x",
                    "source": "netsuite",
                    "is_default": True,
                },
                {
                    "id": "B",
                    "title": "Y",
                    "rationale": "y",
                    "source": "bigquery",
                    "is_default": False,
                },
            ]
        )
        msg["content"] = "Pre-existing prose."
        messages = [
            {"role": "user", "content": "q", "content_summary": None, "tool_calls": None},
            msg,
        ]
        history, _ = build_history_dicts(messages, keep_recent=4)
        clarif = next(h for h in history if h["role"] == "assistant")
        # Existing content survives — synthesized block not injected
        assert clarif["content"].startswith("Pre-existing prose.")
