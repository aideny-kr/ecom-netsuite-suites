# Golden Query Regression Suite — Anti-Hallucination Sprint 4 (TDD)

> 30 known-good question/SQL/tier test cases that run in CI. Catches regressions
> when prompts, models, or patterns change. Tests validate classification logic,
> SQL structure, and judge enforcement without live NetSuite calls.
>
> Use Red-Green-Refactor TDD for each cycle.

Read `CLAUDE.md` before starting. Follow all conventions exactly.

---

## Why This Matters

Today, `test_golden_queries.py` tests intent classification heuristics (69 cases),
NOT actual SQL generation quality. There is no automated way to know if a prompt
change broke a previously working query pattern. The golden regression suite
catches these regressions before they reach production.

## What Exists Today

- `backend/tests/test_golden_queries.py` — 69 intent classification test cases (heuristic rules only)
- `knowledge/golden_dataset/` — 9 markdown files (for RAG, not testing)
- `backend/app/services/importance_classifier.py` — `ImportanceTier` enum + `classify_importance()` (from Sprint 1)
- `backend/app/services/suiteql_judge.py` — `enforce_judge_threshold()` with tier-based thresholds (from Sprint 1)
- `backend/app/mcp/tools/netsuite_suiteql.py` — `is_read_only_sql()`, `validate_query()`, `parse_tables()`
- CI runs `pytest tests/ -v --cov=app --cov-fail-under=60` in `.github/workflows/ci.yml`

## What This Sprint Does NOT Do

- Does NOT execute queries against live NetSuite (no credentials needed)
- Does NOT mock the full LLM agentic loop (too complex, too brittle)
- DOES validate: importance classification, SQL validation, judge threshold enforcement, table extraction

---

## TDD Cycles (5 cycles)

### Phase 1: Fixture File

**Cycle 1 — Golden Query JSON Fixture**

Create `knowledge/golden_queries.json` with 30 test cases across all 4 tiers.

Each test case has:
```json
{
    "id": "gq-001",
    "tier": 1,
    "category": "casual",
    "question": "how many orders today",
    "sample_sql": "SELECT COUNT(*) as cnt FROM transaction WHERE trandate = TRUNC(SYSDATE) AND type = 'SalesOrd'",
    "expected_sql_contains": ["transaction", "COUNT", "trandate"],
    "expected_sql_not_contains": ["DELETE", "UPDATE", "DROP", "INSERT"],
    "expected_tables": ["transaction"],
    "notes": "Simple count, casual tier"
}
```

**Generate these 30 golden queries:**

**Tier 1 — Casual (8 queries):**
```json
[
    {
        "id": "gq-001",
        "tier": 1,
        "category": "casual",
        "question": "how many orders today",
        "sample_sql": "SELECT COUNT(*) as cnt FROM transaction WHERE trandate = TRUNC(SYSDATE) AND type = 'SalesOrd'",
        "expected_sql_contains": ["transaction", "COUNT"],
        "expected_sql_not_contains": ["DELETE", "UPDATE", "DROP"],
        "expected_tables": ["transaction"]
    },
    {
        "id": "gq-002",
        "tier": 1,
        "category": "casual",
        "question": "what is order SO-12345",
        "sample_sql": "SELECT t.id, t.tranid, t.trandate, t.status, t.total FROM transaction t WHERE t.tranid = 'SO-12345'",
        "expected_sql_contains": ["transaction", "tranid"],
        "expected_sql_not_contains": ["DELETE", "UPDATE"],
        "expected_tables": ["transaction"]
    },
    {
        "id": "gq-003",
        "tier": 1,
        "category": "casual",
        "question": "is customer Acme Corp in the system",
        "sample_sql": "SELECT id, companyname, email FROM customer WHERE LOWER(companyname) LIKE '%acme%'",
        "expected_sql_contains": ["customer", "companyname"],
        "expected_sql_not_contains": ["DELETE", "UPDATE"],
        "expected_tables": ["customer"]
    },
    {
        "id": "gq-004",
        "tier": 1,
        "category": "casual",
        "question": "how many items do we have",
        "sample_sql": "SELECT COUNT(*) as item_count FROM item",
        "expected_sql_contains": ["item", "COUNT"],
        "expected_sql_not_contains": ["DELETE"],
        "expected_tables": ["item"]
    },
    {
        "id": "gq-005",
        "tier": 1,
        "category": "casual",
        "question": "who is vendor 12345",
        "sample_sql": "SELECT id, companyname, email FROM vendor WHERE id = 12345",
        "expected_sql_contains": ["vendor"],
        "expected_sql_not_contains": ["DELETE"],
        "expected_tables": ["vendor"]
    },
    {
        "id": "gq-006",
        "tier": 1,
        "category": "casual",
        "question": "any new orders yesterday",
        "sample_sql": "SELECT t.tranid, t.trandate, t.total FROM transaction t WHERE t.trandate = TRUNC(SYSDATE) - 1 AND t.type = 'SalesOrd' ORDER BY t.id DESC FETCH FIRST 20 ROWS ONLY",
        "expected_sql_contains": ["transaction", "SYSDATE"],
        "expected_sql_not_contains": ["DELETE"],
        "expected_tables": ["transaction"]
    },
    {
        "id": "gq-007",
        "tier": 1,
        "category": "casual",
        "question": "latest 5 invoices",
        "sample_sql": "SELECT t.tranid, t.trandate, t.total FROM transaction t WHERE t.type = 'CustInvc' ORDER BY t.id DESC FETCH FIRST 5 ROWS ONLY",
        "expected_sql_contains": ["transaction", "CustInvc", "FETCH FIRST"],
        "expected_sql_not_contains": ["DELETE"],
        "expected_tables": ["transaction"]
    },
    {
        "id": "gq-008",
        "tier": 1,
        "category": "casual",
        "question": "check if PO-9876 exists",
        "sample_sql": "SELECT t.id, t.tranid, t.status FROM transaction t WHERE t.tranid = 'PO-9876' AND t.type = 'PurchOrd'",
        "expected_sql_contains": ["transaction", "PurchOrd"],
        "expected_sql_not_contains": ["DELETE"],
        "expected_tables": ["transaction"]
    }
]
```

**Tier 2 — Operational (8 queries):**
```json
[
    {
        "id": "gq-009",
        "tier": 2,
        "category": "operational",
        "question": "show me unfulfilled orders by vendor",
        "sample_sql": "SELECT BUILTIN.DF(t.entity) as vendor_name, COUNT(*) as order_count FROM transaction t WHERE t.type = 'PurchOrd' AND t.status NOT IN ('G', 'H') GROUP BY BUILTIN.DF(t.entity) ORDER BY order_count DESC",
        "expected_sql_contains": ["transaction", "GROUP BY", "status NOT IN"],
        "expected_sql_not_contains": ["DELETE", "UPDATE"],
        "expected_tables": ["transaction"]
    },
    {
        "id": "gq-010",
        "tier": 2,
        "category": "operational",
        "question": "which customers have pending orders",
        "sample_sql": "SELECT BUILTIN.DF(t.entity) as customer_name, COUNT(*) as pending FROM transaction t WHERE t.type = 'SalesOrd' AND t.status = 'B' GROUP BY BUILTIN.DF(t.entity)",
        "expected_sql_contains": ["transaction", "GROUP BY", "SalesOrd"],
        "expected_sql_not_contains": ["DELETE"],
        "expected_tables": ["transaction"]
    },
    {
        "id": "gq-011",
        "tier": 2,
        "category": "operational",
        "question": "top 10 items by quantity sold this month",
        "sample_sql": "SELECT i.displayname, SUM(tl.quantity) as qty_sold FROM transactionline tl JOIN transaction t ON t.id = tl.transaction JOIN item i ON i.id = tl.item WHERE t.type = 'SalesOrd' AND t.trandate >= TRUNC(SYSDATE, 'MONTH') AND tl.mainline = 'F' GROUP BY i.displayname ORDER BY qty_sold DESC FETCH FIRST 10 ROWS ONLY",
        "expected_sql_contains": ["transactionline", "item", "GROUP BY", "FETCH FIRST"],
        "expected_sql_not_contains": ["DELETE"],
        "expected_tables": ["transaction", "transactionline", "item"]
    },
    {
        "id": "gq-012",
        "tier": 2,
        "category": "operational",
        "question": "list all overdue purchase orders",
        "sample_sql": "SELECT t.tranid, BUILTIN.DF(t.entity) as vendor, t.duedate, t.total FROM transaction t WHERE t.type = 'PurchOrd' AND t.status NOT IN ('G', 'H') AND t.duedate < TRUNC(SYSDATE) ORDER BY t.duedate",
        "expected_sql_contains": ["transaction", "PurchOrd", "duedate"],
        "expected_sql_not_contains": ["DELETE"],
        "expected_tables": ["transaction"]
    },
    {
        "id": "gq-013",
        "tier": 2,
        "category": "operational",
        "question": "show me pending approvals sorted by amount",
        "sample_sql": "SELECT t.tranid, BUILTIN.DF(t.type) as type, t.total, BUILTIN.DF(t.entity) as entity FROM transaction t WHERE t.status = 'A' ORDER BY t.total DESC",
        "expected_sql_contains": ["transaction", "status", "ORDER BY"],
        "expected_sql_not_contains": ["DELETE"],
        "expected_tables": ["transaction"]
    },
    {
        "id": "gq-014",
        "tier": 2,
        "category": "operational",
        "question": "inventory by warehouse for item SKU-100",
        "sample_sql": "SELECT BUILTIN.DF(iil.location) as warehouse, iil.quantityavailable, iil.quantityonhand FROM inventoryitemlocations iil JOIN item i ON i.id = iil.item WHERE i.itemid = 'SKU-100'",
        "expected_sql_contains": ["inventoryitemlocations", "item", "quantityavailable"],
        "expected_sql_not_contains": ["DELETE"],
        "expected_tables": ["inventoryitemlocations", "item"]
    },
    {
        "id": "gq-015",
        "tier": 2,
        "category": "operational",
        "question": "orders grouped by subsidiary this week",
        "sample_sql": "SELECT BUILTIN.DF(t.subsidiary) as subsidiary, COUNT(*) as order_count, SUM(t.total) as total FROM transaction t WHERE t.type = 'SalesOrd' AND t.trandate >= TRUNC(SYSDATE) - 7 GROUP BY BUILTIN.DF(t.subsidiary)",
        "expected_sql_contains": ["transaction", "subsidiary", "GROUP BY"],
        "expected_sql_not_contains": ["DELETE"],
        "expected_tables": ["transaction"]
    },
    {
        "id": "gq-016",
        "tier": 2,
        "category": "operational",
        "question": "compare fulfillment rates between locations",
        "sample_sql": "SELECT BUILTIN.DF(t.location) as loc, COUNT(*) as total_orders, SUM(CASE WHEN t.status = 'G' THEN 1 ELSE 0 END) as fulfilled FROM transaction t WHERE t.type = 'SalesOrd' GROUP BY BUILTIN.DF(t.location)",
        "expected_sql_contains": ["transaction", "location", "GROUP BY"],
        "expected_sql_not_contains": ["DELETE"],
        "expected_tables": ["transaction"]
    }
]
```

**Tier 3 — Reporting (8 queries):**
```json
[
    {
        "id": "gq-017",
        "tier": 3,
        "category": "reporting",
        "question": "total revenue by month this quarter",
        "sample_sql": "SELECT TO_CHAR(t.trandate, 'YYYY-MM') as month, SUM(t.total) as revenue FROM transaction t WHERE t.type = 'CustInvc' AND t.trandate >= ADD_MONTHS(TRUNC(SYSDATE, 'Q'), 0) GROUP BY TO_CHAR(t.trandate, 'YYYY-MM') ORDER BY month",
        "expected_sql_contains": ["transaction", "SUM", "GROUP BY", "CustInvc"],
        "expected_sql_not_contains": ["DELETE", "UPDATE"],
        "expected_tables": ["transaction"]
    },
    {
        "id": "gq-018",
        "tier": 3,
        "category": "reporting",
        "question": "monthly sales summary for the dashboard",
        "sample_sql": "SELECT TO_CHAR(t.trandate, 'YYYY-MM') as month, COUNT(*) as order_count, SUM(t.total) as total_sales FROM transaction t WHERE t.type = 'SalesOrd' AND t.trandate >= ADD_MONTHS(TRUNC(SYSDATE), -6) GROUP BY TO_CHAR(t.trandate, 'YYYY-MM') ORDER BY month",
        "expected_sql_contains": ["transaction", "SUM", "GROUP BY"],
        "expected_sql_not_contains": ["DELETE"],
        "expected_tables": ["transaction"]
    },
    {
        "id": "gq-019",
        "tier": 3,
        "category": "reporting",
        "question": "quarterly revenue trend year over year",
        "sample_sql": "SELECT TO_CHAR(t.trandate, 'YYYY') as year, TO_CHAR(t.trandate, 'Q') as quarter, SUM(t.total) as revenue FROM transaction t WHERE t.type = 'CustInvc' GROUP BY TO_CHAR(t.trandate, 'YYYY'), TO_CHAR(t.trandate, 'Q') ORDER BY year, quarter",
        "expected_sql_contains": ["transaction", "SUM", "GROUP BY", "CustInvc"],
        "expected_sql_not_contains": ["DELETE"],
        "expected_tables": ["transaction"]
    },
    {
        "id": "gq-020",
        "tier": 3,
        "category": "reporting",
        "question": "KPI report: average order value by department",
        "sample_sql": "SELECT BUILTIN.DF(t.department) as dept, COUNT(*) as orders, SUM(t.total) as revenue, ROUND(SUM(t.total)/COUNT(*), 2) as avg_order FROM transaction t WHERE t.type = 'SalesOrd' GROUP BY BUILTIN.DF(t.department)",
        "expected_sql_contains": ["transaction", "department", "GROUP BY", "SUM"],
        "expected_sql_not_contains": ["DELETE"],
        "expected_tables": ["transaction"]
    },
    {
        "id": "gq-021",
        "tier": 3,
        "category": "reporting",
        "question": "sales by item category for the monthly report",
        "sample_sql": "SELECT BUILTIN.DF(tl.class) as category, SUM(tl.amount * -1) as revenue FROM transactionline tl JOIN transaction t ON t.id = tl.transaction WHERE t.type = 'CustInvc' AND tl.mainline = 'F' AND tl.taxline = 'F' GROUP BY BUILTIN.DF(tl.class) ORDER BY revenue DESC",
        "expected_sql_contains": ["transactionline", "transaction", "GROUP BY", "SUM"],
        "expected_sql_not_contains": ["DELETE"],
        "expected_tables": ["transaction", "transactionline"]
    },
    {
        "id": "gq-022",
        "tier": 3,
        "category": "reporting",
        "question": "expense forecast vs actuals by month",
        "sample_sql": "SELECT TO_CHAR(t.trandate, 'YYYY-MM') as month, SUM(t.total) as actual_expense FROM transaction t WHERE t.type = 'VendBill' GROUP BY TO_CHAR(t.trandate, 'YYYY-MM') ORDER BY month",
        "expected_sql_contains": ["transaction", "VendBill", "SUM", "GROUP BY"],
        "expected_sql_not_contains": ["DELETE"],
        "expected_tables": ["transaction"]
    },
    {
        "id": "gq-023",
        "tier": 3,
        "category": "reporting",
        "question": "export customer purchase history for the quarterly review",
        "sample_sql": "SELECT BUILTIN.DF(t.entity) as customer, COUNT(*) as orders, SUM(t.total) as total_spent FROM transaction t WHERE t.type = 'CustInvc' AND t.trandate >= ADD_MONTHS(TRUNC(SYSDATE, 'Q'), 0) GROUP BY BUILTIN.DF(t.entity) ORDER BY total_spent DESC",
        "expected_sql_contains": ["transaction", "CustInvc", "GROUP BY", "SUM"],
        "expected_sql_not_contains": ["DELETE"],
        "expected_tables": ["transaction"]
    },
    {
        "id": "gq-024",
        "tier": 3,
        "category": "reporting",
        "question": "inventory turnover report by location",
        "sample_sql": "SELECT BUILTIN.DF(iil.location) as location, COUNT(*) as items, SUM(iil.quantityonhand) as total_onhand FROM inventoryitemlocations iil WHERE iil.quantityonhand > 0 GROUP BY BUILTIN.DF(iil.location) ORDER BY total_onhand DESC",
        "expected_sql_contains": ["inventoryitemlocations", "GROUP BY", "quantityonhand"],
        "expected_sql_not_contains": ["DELETE"],
        "expected_tables": ["inventoryitemlocations"]
    }
]
```

**Tier 4 — Audit Critical (6 queries):**
```json
[
    {
        "id": "gq-025",
        "tier": 4,
        "category": "audit_critical",
        "question": "net income by account for Q4 audit",
        "sample_sql": "SELECT a.acctnumber, a.acctname, BUILTIN.DF(a.accttype) as account_type, SUM(tal.amount) as net_amount FROM transactionaccountingline tal JOIN transaction t ON t.id = tal.transaction JOIN account a ON a.id = tal.account WHERE t.trandate BETWEEN TO_DATE('2025-10-01','YYYY-MM-DD') AND TO_DATE('2025-12-31','YYYY-MM-DD') GROUP BY a.acctnumber, a.acctname, BUILTIN.DF(a.accttype) ORDER BY a.acctnumber",
        "expected_sql_contains": ["transactionaccountingline", "account", "GROUP BY", "SUM"],
        "expected_sql_not_contains": ["DELETE", "UPDATE", "DROP"],
        "expected_tables": ["transactionaccountingline", "account", "transaction"]
    },
    {
        "id": "gq-026",
        "tier": 4,
        "category": "audit_critical",
        "question": "P&L by department for board presentation",
        "sample_sql": "SELECT BUILTIN.DF(tal.department) as department, BUILTIN.DF(a.accttype) as type, SUM(tal.amount) as amount FROM transactionaccountingline tal JOIN account a ON a.id = tal.account JOIN transaction t ON t.id = tal.transaction WHERE a.accttype IN ('Income', 'COGS', 'Expense') GROUP BY BUILTIN.DF(tal.department), BUILTIN.DF(a.accttype)",
        "expected_sql_contains": ["transactionaccountingline", "account", "accttype", "GROUP BY"],
        "expected_sql_not_contains": ["DELETE", "UPDATE"],
        "expected_tables": ["transactionaccountingline", "account", "transaction"]
    },
    {
        "id": "gq-027",
        "tier": 4,
        "category": "audit_critical",
        "question": "balance sheet accounts for year-end close",
        "sample_sql": "SELECT a.acctnumber, a.acctname, BUILTIN.DF(a.accttype) as type, SUM(tal.debit) as total_debit, SUM(tal.credit) as total_credit FROM transactionaccountingline tal JOIN account a ON a.id = tal.account WHERE a.accttype IN ('Bank', 'AcctRec', 'OthCurrAsset', 'FixedAsset', 'AcctPay', 'OthCurrLiab', 'LongTermLiab', 'Equity') GROUP BY a.acctnumber, a.acctname, BUILTIN.DF(a.accttype) ORDER BY a.acctnumber",
        "expected_sql_contains": ["transactionaccountingline", "account", "debit", "credit", "GROUP BY"],
        "expected_sql_not_contains": ["DELETE", "UPDATE"],
        "expected_tables": ["transactionaccountingline", "account"]
    },
    {
        "id": "gq-028",
        "tier": 4,
        "category": "audit_critical",
        "question": "revenue recognition report for SEC filing",
        "sample_sql": "SELECT TO_CHAR(t.trandate, 'YYYY-MM') as month, BUILTIN.DF(t.subsidiary) as subsidiary, SUM(tl.amount * -1) as recognized_revenue FROM transactionline tl JOIN transaction t ON t.id = tl.transaction WHERE t.type = 'CustInvc' AND tl.mainline = 'F' AND tl.taxline = 'F' GROUP BY TO_CHAR(t.trandate, 'YYYY-MM'), BUILTIN.DF(t.subsidiary) ORDER BY month, subsidiary",
        "expected_sql_contains": ["transactionline", "transaction", "CustInvc", "SUM", "GROUP BY"],
        "expected_sql_not_contains": ["DELETE", "UPDATE"],
        "expected_tables": ["transaction", "transactionline"]
    },
    {
        "id": "gq-029",
        "tier": 4,
        "category": "audit_critical",
        "question": "cash flow statement for investor deck",
        "sample_sql": "SELECT a.acctnumber, a.acctname, SUM(tal.debit) as debit, SUM(tal.credit) as credit, SUM(tal.amount) as net FROM transactionaccountingline tal JOIN account a ON a.id = tal.account JOIN transaction t ON t.id = tal.transaction WHERE a.accttype = 'Bank' GROUP BY a.acctnumber, a.acctname ORDER BY a.acctnumber",
        "expected_sql_contains": ["transactionaccountingline", "account", "Bank", "GROUP BY"],
        "expected_sql_not_contains": ["DELETE"],
        "expected_tables": ["transactionaccountingline", "account", "transaction"]
    },
    {
        "id": "gq-030",
        "tier": 4,
        "category": "audit_critical",
        "question": "gross margin by product line for materiality assessment",
        "sample_sql": "SELECT BUILTIN.DF(tl.class) as product_line, SUM(CASE WHEN a.accttype = 'Income' THEN tal.amount ELSE 0 END) as revenue, SUM(CASE WHEN a.accttype = 'COGS' THEN tal.amount ELSE 0 END) as cogs FROM transactionaccountingline tal JOIN account a ON a.id = tal.account JOIN transaction t ON t.id = tal.transaction JOIN transactionline tl ON tl.transaction = t.id GROUP BY BUILTIN.DF(tl.class)",
        "expected_sql_contains": ["transactionaccountingline", "account", "Income", "COGS", "GROUP BY"],
        "expected_sql_not_contains": ["DELETE"],
        "expected_tables": ["transactionaccountingline", "account", "transaction"]
    }
]
```

---

### Phase 2: Test Suite

**Cycle 2 — Fixture Helpers**

RED — Test the fixture loads and validates:
```python
def test_golden_queries_load():
    from tests.fixtures.golden_queries import load_golden_queries
    queries = load_golden_queries()
    assert len(queries) == 30

def test_golden_query_schema_valid():
    from tests.fixtures.golden_queries import load_golden_queries, validate_schema
    queries = load_golden_queries()
    for q in queries:
        validate_schema(q)  # Should not raise
```

GREEN — Create `backend/tests/fixtures/golden_queries.py`:
```python
"""Helpers for loading and validating golden query test fixtures."""

import json
from pathlib import Path

_FIXTURE_PATH = Path(__file__).resolve().parents[2] / "knowledge" / "golden_queries.json"

_REQUIRED_FIELDS = {"id", "tier", "category", "question", "sample_sql", "expected_sql_contains", "expected_sql_not_contains", "expected_tables"}


def load_golden_queries() -> list[dict]:
    """Load golden queries from JSON fixture."""
    with open(_FIXTURE_PATH) as f:
        return json.load(f)


def validate_schema(query: dict) -> None:
    """Validate a golden query has all required fields."""
    missing = _REQUIRED_FIELDS - set(query.keys())
    if missing:
        raise ValueError(f"Golden query {query.get('id', '?')} missing fields: {missing}")
    assert 1 <= query["tier"] <= 4, f"Invalid tier: {query['tier']}"
    assert query["category"] in ("casual", "operational", "reporting", "audit_critical")
    assert len(query["expected_sql_contains"]) > 0
    assert len(query["expected_tables"]) > 0
```

REFACTOR: None needed.

---

**Cycle 3 — Importance Tier Validation Tests**

RED — Create `backend/tests/test_golden_query_regression.py`:
```python
import pytest
from tests.fixtures.golden_queries import load_golden_queries, validate_schema
from app.services.importance_classifier import classify_importance, ImportanceTier

GOLDEN_QUERIES = load_golden_queries()


class TestGoldenQuerySchema:
    """Validate the fixture file itself."""

    def test_fixture_has_30_queries(self):
        assert len(GOLDEN_QUERIES) == 30

    @pytest.mark.parametrize("spec", GOLDEN_QUERIES, ids=lambda s: s["id"])
    def test_schema_valid(self, spec):
        validate_schema(spec)

    def test_tier_distribution(self):
        tiers = [q["tier"] for q in GOLDEN_QUERIES]
        assert tiers.count(1) >= 6, "Need at least 6 casual queries"
        assert tiers.count(2) >= 6, "Need at least 6 operational queries"
        assert tiers.count(3) >= 6, "Need at least 6 reporting queries"
        assert tiers.count(4) >= 4, "Need at least 4 audit-critical queries"


class TestGoldenQueryImportance:
    """Verify importance classifier assigns correct tier for each golden query."""

    @pytest.mark.parametrize("spec", GOLDEN_QUERIES, ids=lambda s: s["id"])
    def test_importance_tier(self, spec):
        tier = classify_importance(spec["question"])
        assert tier.value == spec["tier"], (
            f"Query '{spec['question']}' classified as {tier.label} (tier {tier.value}), "
            f"expected tier {spec['tier']} ({ImportanceTier(spec['tier']).label})"
        )
```

GREEN — All importance classifications should pass if Sprint 1 regex patterns are correct.
If any fail, update the regex patterns in `importance_classifier.py` to handle the edge case.

REFACTOR: None needed.

---

**Cycle 4 — SQL Structure Validation Tests**

RED — Add to `test_golden_query_regression.py`:
```python
from app.mcp.tools.netsuite_suiteql import is_read_only_sql, parse_tables


class TestGoldenQuerySQL:
    """Validate sample SQL structure for each golden query."""

    @pytest.mark.parametrize("spec", GOLDEN_QUERIES, ids=lambda s: s["id"])
    def test_sql_is_read_only(self, spec):
        """All golden queries must be read-only SELECT statements."""
        assert is_read_only_sql(spec["sample_sql"]), (
            f"Golden query {spec['id']} SQL is not read-only: {spec['sample_sql'][:100]}"
        )

    @pytest.mark.parametrize("spec", GOLDEN_QUERIES, ids=lambda s: s["id"])
    def test_sql_contains_expected(self, spec):
        """SQL should contain all expected keywords/patterns."""
        sql_upper = spec["sample_sql"].upper()
        for keyword in spec["expected_sql_contains"]:
            assert keyword.upper() in sql_upper, (
                f"Golden query {spec['id']} SQL missing '{keyword}': "
                f"{spec['sample_sql'][:200]}"
            )

    @pytest.mark.parametrize("spec", GOLDEN_QUERIES, ids=lambda s: s["id"])
    def test_sql_not_contains_forbidden(self, spec):
        """SQL should NOT contain forbidden keywords."""
        sql_upper = spec["sample_sql"].upper()
        for keyword in spec["expected_sql_not_contains"]:
            assert keyword.upper() not in sql_upper, (
                f"Golden query {spec['id']} SQL contains forbidden '{keyword}': "
                f"{spec['sample_sql'][:200]}"
            )

    @pytest.mark.parametrize("spec", GOLDEN_QUERIES, ids=lambda s: s["id"])
    def test_sql_references_expected_tables(self, spec):
        """SQL should reference the expected tables."""
        tables = parse_tables(spec["sample_sql"])
        for expected_table in spec["expected_tables"]:
            assert expected_table.lower() in tables, (
                f"Golden query {spec['id']} SQL doesn't reference table '{expected_table}'. "
                f"Found tables: {tables}"
            )

    @pytest.mark.parametrize("spec", GOLDEN_QUERIES, ids=lambda s: s["id"])
    def test_sql_uses_single_letter_status_codes(self, spec):
        """SQL should use single-letter status codes, not compound ones."""
        import re
        compound_pattern = re.compile(r"(?:SalesOrd|PurchOrd|CustInvc|VendBill):[A-Z]")
        match = compound_pattern.search(spec["sample_sql"])
        assert match is None, (
            f"Golden query {spec['id']} uses compound status code: {match.group()}. "
            f"Use single-letter codes instead."
        )
```

GREEN — All sample SQL in the fixture should already follow SuiteQL best practices.

REFACTOR: None needed.

---

**Cycle 5 — Judge Threshold Validation Tests**

RED — Add to `test_golden_query_regression.py`:
```python
from app.services.suiteql_judge import enforce_judge_threshold, EnforcementResult, JudgeVerdict


class TestGoldenQueryJudge:
    """Validate judge enforcement thresholds for each tier.

    NOTE: enforce_judge_threshold() returns a frozen dataclass `EnforcementResult`,
    not a dict. Use dot notation: result.passed, result.needs_review, result.tier, result.reason.
    """

    @pytest.mark.parametrize(
        "spec",
        [q for q in GOLDEN_QUERIES if q["tier"] >= 2],
        ids=lambda s: s["id"],
    )
    def test_tier_2_plus_has_judge_threshold(self, spec):
        """Tier 2+ queries should have meaningful judge thresholds."""
        tier = ImportanceTier(spec["tier"])
        assert tier.judge_confidence_threshold > 0, (
            f"Tier {spec['tier']} should have a non-zero judge threshold"
        )

    def test_casual_passes_with_low_confidence(self):
        """Tier 1 queries should always pass regardless of confidence."""
        verdict = JudgeVerdict(approved=True, confidence=0.2, reason="Low confidence")
        result = enforce_judge_threshold(verdict, ImportanceTier.CASUAL)
        assert isinstance(result, EnforcementResult)
        assert result.passed is True

    def test_operational_fails_below_threshold(self):
        """Tier 2 queries should fail below 0.6 confidence."""
        verdict = JudgeVerdict(approved=True, confidence=0.5, reason="Moderate")
        result = enforce_judge_threshold(verdict, ImportanceTier.OPERATIONAL)
        assert result.passed is False

    def test_reporting_fails_below_threshold(self):
        """Tier 3 queries should fail below 0.8 confidence."""
        verdict = JudgeVerdict(approved=True, confidence=0.7, reason="Good but not great")
        result = enforce_judge_threshold(verdict, ImportanceTier.REPORTING)
        assert result.passed is False

    def test_audit_critical_flags_for_review(self):
        """Tier 4 queries below threshold should flag for human review."""
        verdict = JudgeVerdict(approved=True, confidence=0.85, reason="Pretty good")
        result = enforce_judge_threshold(verdict, ImportanceTier.AUDIT_CRITICAL)
        assert result.passed is False
        assert result.needs_review is True

    def test_audit_critical_passes_high_confidence(self):
        """Tier 4 queries with high confidence should pass."""
        verdict = JudgeVerdict(approved=True, confidence=0.95, reason="Excellent")
        result = enforce_judge_threshold(verdict, ImportanceTier.AUDIT_CRITICAL)
        assert result.passed is True
        assert result.needs_review is False
```

GREEN — These tests validate Sprint 1's `enforce_judge_threshold()` against golden query tiers.

REFACTOR: None needed.

---

## Files to Create (3 new)

| File | Purpose |
|------|---------|
| `knowledge/golden_queries.json` | 30 test cases across 4 tiers |
| `backend/tests/test_golden_query_regression.py` | Parametrized regression test suite |
| `backend/tests/fixtures/golden_queries.py` | Fixture loading and validation helpers |

## Files to Modify (0)

No existing files need modification. The CI workflow already runs `pytest tests/` which will
automatically pick up the new test file.

## Dependencies

- Sprint 1 (query importance ranking) must be implemented first. The regression tests
  validate `classify_importance()` and `enforce_judge_threshold()`.
- `is_read_only_sql()` and `parse_tables()` from `netsuite_suiteql.py` (already exist)

## Verification

1. `pytest backend/tests/test_golden_query_regression.py -v` — all 30 queries pass all test classes
2. `pytest backend/tests/test_golden_query_regression.py -v --tb=short` — verify output is clean
3. Expected output:
```
test_golden_query_regression.py::TestGoldenQuerySchema::test_fixture_has_30_queries PASSED
test_golden_query_regression.py::TestGoldenQuerySchema::test_schema_valid[gq-001] PASSED
...
test_golden_query_regression.py::TestGoldenQueryImportance::test_importance_tier[gq-001] PASSED
...
test_golden_query_regression.py::TestGoldenQuerySQL::test_sql_is_read_only[gq-001] PASSED
...
test_golden_query_regression.py::TestGoldenQueryJudge::test_casual_passes_with_low_confidence PASSED
...
========== 120+ passed in 2.5s ==========
```
4. To test regression detection: temporarily change a golden query's expected tier → test should fail
5. Push to PR → CI catches it automatically
