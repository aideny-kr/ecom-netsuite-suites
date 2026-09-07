"""Task 3 -- pure projections over synced Celigo objects (no DB, no I/O).

See `app/services/celigo/topology.py`'s own module docstring for why this
module exists as the one place that reads a flow's declared router/branch
shape and a script's clone-family state."""

import uuid
from datetime import datetime, timedelta, timezone

from app.models.celigo import CeligoScript
from app.services.celigo.topology import (
    adaptor_family,
    assign_version_letters,
    count_rules,
    project_routers,
    script_family_facts,
    step_kind,
)

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


def _script(dedup_key, content_hash, content, modified, *, id=None):
    return CeligoScript(
        id=id if id is not None else uuid.uuid4(),
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


def test_assign_version_letters_ties_on_first_seen_broken_by_member_id_not_hash():
    """Review finding (Task 1 round 1): the extracted `assign_version_letters`
    must be byte-identical to the pre-extraction inline algorithm it replaced
    -- which broke a tie between two DISTINCT content hashes sharing the same
    earliest `celigo_last_modified` by the member's own row id (`str(s.id)`),
    never by the hash string. Two members here share `modified=1`; their ids
    are chosen so the hash-string order ("aaaa_hash" < "zzzz_hash") would
    pick the OPPOSITE winner from the id order, so this pins the real rule
    rather than one that happens to agree with both orderings."""
    lower_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    higher_id = uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
    # zzzz_hash's member has the LOWER id, so id-order picks zzzz_hash first --
    # the opposite of hash-string order, which would pick aaaa_hash first.
    zzzz_member = _script("k", "zzzz_hash", "a" * 10, 1, id=lower_id)
    aaaa_member = _script("k", "aaaa_hash", "b" * 10, 1, id=higher_id)

    letters = assign_version_letters([zzzz_member, aaaa_member])

    assert letters == {"zzzz_hash": "A", "aaaa_hash": "B"}

    # Order-independence: the docstring claims the result is "independent of
    # row-insertion order" -- this reverses the input list and checks the
    # SAME letters come back, which the test never actually verified before
    # this review finding (it only ever passed members in one fixed order).
    letters_reversed = assign_version_letters([aaaa_member, zzzz_member])
    assert letters_reversed == letters


def _spreadsheet_column(n: int) -> str:
    """The expected letter for 0-based index *n*, computed independently of
    `assign_version_letters` itself (bijective base-26: 0->A, 25->Z,
    26->AA, 27->AB, ... 51->AZ, 52->BA, ...) -- used below as the oracle,
    not copy-pasted from the implementation under test."""
    n += 1
    letters = ""
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _scripts_with_n_distinct_hashes(n: int) -> list[CeligoScript]:
    """*n* members of one family, each carrying a distinct `content_hash`
    and a strictly increasing `celigo_last_modified` -- so hash `i` is
    first-seen at position `i` and must be assigned letter `i` (0-based, in
    spreadsheet order)."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        CeligoScript(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            celigo_connection_id=uuid.uuid4(),
            celigo_id=str(uuid.uuid4()),
            name="fam",
            dedup_key="k",
            content_hash=f"h{i:04d}",
            content=f"content-{i}",
            celigo_last_modified=base + timedelta(minutes=i),
        )
        for i in range(n)
    ]


def test_assign_version_letters_goes_past_z_spreadsheet_style_with_27_hashes():
    """Review finding (brief item 3): `chr(ord("A") + i)` breaks past the
    26th hash (`chr(ord("A") + 26)` is `"["`, not a letter at all). 27
    distinct hashes must produce A..Z then AA, spreadsheet-column style."""
    members = _scripts_with_n_distinct_hashes(27)

    letters = assign_version_letters(members)

    assert letters["h0000"] == "A"
    assert letters["h0025"] == "Z"
    assert letters["h0026"] == "AA"
    assert letters == {f"h{i:04d}": _spreadsheet_column(i) for i in range(27)}


def test_assign_version_letters_goes_past_z_spreadsheet_style_with_53_hashes():
    members = _scripts_with_n_distinct_hashes(53)

    letters = assign_version_letters(members)

    assert letters["h0025"] == "Z"
    assert letters["h0026"] == "AA"
    assert letters["h0051"] == "AZ"
    assert letters["h0052"] == "BA"
    assert letters == {f"h{i:04d}": _spreadsheet_column(i) for i in range(53)}


def test_adaptor_family_groups_case_insensitively_netsuite_first():
    assert adaptor_family("NetSuiteExport") == "NetSuite"
    assert adaptor_family("NetSuiteDistributedImport") == "NetSuite"
    assert adaptor_family("netsuite_da") == "NetSuite"
    assert adaptor_family("AS2Export") == "AS2"
    assert adaptor_family("FTPImport") == "FTP"
    assert adaptor_family("RDBMSExport") == "RDBMS"
    assert adaptor_family("RESTImport") == "REST"
    assert adaptor_family("HTTPExport") == "HTTP"


def test_adaptor_family_unknown_and_none_are_none():
    assert adaptor_family("SmartsheetExport") is None
    assert adaptor_family(None) is None
