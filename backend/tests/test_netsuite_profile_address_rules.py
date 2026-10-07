"""Guard the netsuite.yaml address-query rules — the soft 'both work' guidance let
the agent filter with BUILTIN.DF (a per-row function) on an unbounded address join,
which timed out (2026-06). These assert the strengthened rules stay in the profile.
"""

from pathlib import Path

import app.services.chat.knowledge_profiles as kp


def _netsuite_yaml() -> str:
    return (Path(kp.__file__).parent / "netsuite.yaml").read_text()


def _join_patterns_doc() -> str:
    # knowledge/ lives at the repo/worktree root (backend/tests -> parents[2]).
    root = Path(__file__).resolve().parents[2]
    return (root / "knowledge" / "golden_dataset" / "join-patterns-and-aggregation.md").read_text()


def test_forbids_builtin_df_in_where_for_filtering():
    text = _netsuite_yaml()
    assert "NEVER `BUILTIN.DF(...)` in a WHERE" in text, "address rules must forbid BUILTIN.DF in WHERE (perf killer)"


def test_translates_country_names_to_iso():
    text = _netsuite_yaml()
    assert "Translate user country NAMES to ISO-2" in text


def test_requires_date_scope_on_address_joins():
    text = _netsuite_yaml()
    assert "Address-table joins are HEAVY" in text
    assert "t.trandate" in text


def test_address_rule_says_an_upper_limit_alone_is_still_all_history():
    # vs-MCP 2026-10-06: "as of today" became `t.trandate <= today` over the address join and
    # timed out on both sides. The rule must name the lower limit, and say the tool refuses it.
    rule = next(line for line in _netsuite_yaml().splitlines() if "Address-table joins are HEAVY" in line)
    assert "lower limit" in rule and "t.trandate <=" in rule and "refuses" in rule
    # #397 review R1: "as of today" can mean cumulative-to-date; never silently narrow it to today.
    assert "as of today" not in rule.lower() or "do not narrow" in rule.lower()


def test_warns_shipcountry_is_not_exposed():
    # transaction.shipcountry is NOT_EXPOSED — the rules must flag it, not recommend
    # it as an escape hatch (codex review, 2026-06-05).
    text = _netsuite_yaml()
    assert "t.shipcountry" in text
    assert "NOT_EXPOSED" in text


def test_shipcountry_warning_is_not_tenant_specific():
    # De-pollution: the global profile must NOT bake in "on this account" tenant-
    # specific schema (grill, 2026-06-05). Phrase it generically instead.
    text = _netsuite_yaml()
    assert "on this account" not in text


def test_custom_list_rule_has_perf_caveat():
    # The custom-list BUILTIN.DF(field)='Value' allowance must carry a perf caveat so
    # it no longer contradicts the ADDRESS TABLES "NEVER BUILTIN.DF in WHERE" rule
    # (grill: the blanket allowance was the seed template for the country anti-pattern).
    text = _netsuite_yaml()
    # 2026-10-05: "OK for small static lists" read as permission to filter a platform list
    # through BUILTIN.DF on an undated transactionline scan (65-116 s per query, a 7-minute
    # turn). The caveat must name what actually makes it slow: the undated line scan.
    rule = next(line for line in text.splitlines() if "SELECT-type fields store integer IDs" in line)
    assert "small static lists" not in rule.lower()
    assert "transactionline" in rule and "trandate" in rule


def test_join_patterns_doc_drops_both_work_country_framing():
    # The soft "Both work" country guidance let the agent filter with BUILTIN.DF on the
    # address join (2026-06 timeout). The reconciled doc must not teach it as equivalent.
    doc = _join_patterns_doc()
    assert "Both work" not in doc


def test_join_patterns_doc_date_scopes_the_address_join():
    # The canonical join-pattern query must carry a trandate scope, or it still
    # full-scans all-time and times out on large accounts.
    doc = _join_patterns_doc()
    assert "t.trandate" in doc
