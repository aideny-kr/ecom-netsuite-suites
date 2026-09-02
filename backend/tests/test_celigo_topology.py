"""Task 3 -- pure projections over synced Celigo objects (no DB, no I/O).

See `app/services/celigo/topology.py`'s own module docstring for why this
module exists as the one place that reads a flow's declared router/branch
shape and a script's clone-family state."""

import uuid
from datetime import datetime, timezone

from app.models.celigo import CeligoScript
from app.services.celigo.topology import count_rules, project_routers, script_family_facts, step_kind

MULTI_SUB_RAW = {
    "routers": [
        {
            "id": "3e2jFK0ax5e",
            "name": "",
            "branches": [
                {
                    "branchId": "170BOshDuyE",
                    "name": "",
                    "nextRouterId": "uxtwub0B7rh",
                    "pageProcessors": [{"_exportId": "lkp"}],
                },
            ],
        },
        {
            "id": "uxtwub0B7rh",
            "name": "",
            "routeRecordsTo": "first_matching_branch",
            "routeRecordsUsing": "input_filters",
            "script": {"function": "branching"},
            "branches": [
                {
                    "branchId": "J7gXUjQIzH4",
                    "name": "Framework Intl",
                    "inputFilter": {
                        "rules": ["notequals", ["string", ["extract", "business_entity"]], "Framework Inc"]
                    },
                    "pageProcessors": [{}, {}, {}, {}],
                },
                {
                    "branchId": "OMcnSSbNoaU",
                    "name": "Framework Inc",
                    "inputFilter": {"rules": ["equals", ["string", ["extract", "business_entity"]], "Framework Inc"]},
                    "pageProcessors": [{}, {}, {}, {}],
                },
            ],
        },
    ]
}


def test_step_kind_follows_celigo_vocabulary():
    assert step_kind("generator", "HTTPExport") == "source"
    assert step_kind("processor", "NetSuiteExport") == "lookup"
    assert step_kind("processor", "HTTPExport") == "lookup"
    assert step_kind("processor", "NetSuiteDistributedImport") == "destination"
    assert step_kind("processor", None) == "destination"


def test_count_rules_counts_one_expression_as_one_rule():
    assert count_rules(None) == 0
    assert count_rules([]) == 0
    assert count_rules(["notequals", ["string", ["extract", "x"]], "y"]) == 1
    assert count_rules(["and", ["equals", "a", "b"], ["equals", "c", "d"]]) == 2
    assert count_rules("garbage") == 0


def test_project_routers_keeps_declared_order_chain_names_and_rule_counts():
    routers = project_routers(MULTI_SUB_RAW)
    assert [r["id"] for r in routers] == ["3e2jFK0ax5e", "uxtwub0B7rh"]
    first, second = routers
    assert first["route_records_to"] is None and first["has_script_slot"] is False
    assert first["branches"] == [
        {
            "id": "170BOshDuyE",
            "name": None,
            "rule_count": 0,
            "next_router_id": "uxtwub0B7rh",
            "order": 0,
            "declared_step_count": 1,
        }
    ]
    assert second["route_records_to"] == "first_matching_branch"
    assert second["route_records_using"] == "input_filters"
    assert second["has_script_slot"] is True
    assert [b["name"] for b in second["branches"]] == ["Framework Intl", "Framework Inc"]
    assert [b["rule_count"] for b in second["branches"]] == [1, 1]
    assert [b["order"] for b in second["branches"]] == [0, 1]


def test_project_routers_tolerates_missing_or_malformed():
    assert project_routers({}) == []
    assert project_routers({"routers": "nope"}) == []
    assert project_routers({"routers": [{"id": "r", "branches": [None, {"branchId": "b"}]}]})[0]["branches"] == [
        {"id": "b", "name": None, "rule_count": 0, "next_router_id": None, "order": 1, "declared_step_count": 0}
    ]


def _script(dedup_key, content_hash, content, modified):
    return CeligoScript(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        celigo_connection_id=uuid.uuid4(),
        celigo_id=str(uuid.uuid4()),
        name="ns_sales_order_premap",
        dedup_key=dedup_key,
        content_hash=content_hash,
        content=content,
        celigo_last_modified=datetime(2026, 1, modified, tzinfo=timezone.utc),
    )


def test_script_family_facts_letters_versions_by_first_seen():
    fam = [
        _script("k", "h1", "a" * 10, 1),
        _script("k", "h2", "b" * 20, 2),
        _script("k", "h2", "b" * 20, 3),
        _script("k", "h3", "c" * 30, 4),
    ]
    facts = script_family_facts(fam)
    assert (
        facts[fam[0].id].version_letter == "A"
        and facts[fam[0].id].copies_count == 4
        and facts[fam[0].id].versions_count == 3
    )
    assert facts[fam[2].id].version_letter == "B" and facts[fam[2].id].content_diverged is True
    assert facts[fam[3].id].version_letter == "C" and facts[fam[3].id].size_chars == 30


def test_script_family_facts_single_copy_has_no_letter_and_is_not_diverged():
    s = _script("solo", "h", "x", 1)
    f = script_family_facts([s])[s.id]
    assert (f.copies_count, f.versions_count, f.version_letter, f.content_diverged) == (1, 1, None, False)
