"""G1: one treatment registry replaces every hand-maintained set of correction kinds.

The group-card crash in PR #262 was a proposal meeting a branch that inferred its
transport from its kind. Grouping, rules fingerprints, lock keys, recheck targets,
verification dispatch and recovery queries each kept their own copy of the kind
sets. This registry is the single definition; the last test proves no other copy
exists in the application tree.
"""

import ast
from pathlib import Path

import pytest

from app.services.transaction_ops import accounting_recheck, treatments
from app.services.transaction_ops.treatments import (
    KINDS,
    REGISTRY,
    collision_key,
    reconciliation_target_id,
    treatment_of,
    treatment_profile,
)
from tests.test_accounting_release_regressions import mcp_credit  # noqa: F401

APP = Path(__file__).resolve().parents[1] / "app"


def _proposal(kind, **extra):
    base = {
        "kind": kind,
        "record_type": {"sales_adjustment_credit": "creditmemo", "credit_tax_reallocation": "creditmemo"}.get(
            kind, "salesorder" if kind and kind.startswith("sales_order") else "invoice"
        ),
        "record_id": "900",
        "invoice_id": "22",
        "sales_order_id": "12",
        "before": {"createdFrom": {"id": "12"}},
        "support": {"invoice": {"createdFrom": {"id": "12"}}},
        "profile": {"currency": "USD"},
        "native_profile": {"schema_version": 1},
        "connector_schema": {"fields_digest": "abc"},
        "tax_item": {"id": "7"},
    }
    base.update(extra)
    return base


def test_registry_covers_every_kind_with_the_attributes_the_call_sites_need():
    assert (
        set(KINDS)
        == set(REGISTRY)
        == {
            "invoice_tax",
            "invoice_sales_adjustment",
            "sales_adjustment_credit",
            "sales_order_source_alignment",
            "credit_tax_reallocation",
            "sales_order_line_alignment",
        }
    )
    for kind, t in REGISTRY.items():
        assert t.kind == kind and KINDS[kind] == t.label
        assert t.record_type in {"invoice", "creditmemo", "salesorder"}
        assert t.family in {"invoice_tax", "commercial", "amendment"}
        assert t.lock in {"record", "invoice", "invoice_record"}
        assert t.reconciliation_target in {"created_from", "record", "sales_order"}
        assert t.verification in {"invoice", "discount", "credit", "order", "amendment"}


def test_missing_kind_is_the_legacy_invoice_tax_treatment():
    assert treatment_of({}).kind == "invoice_tax"
    assert treatment_of({"kind": None}).kind == "invoice_tax"
    with pytest.raises(KeyError):
        treatment_of({"kind": "tax_reversal"})


@pytest.mark.parametrize(
    "kind,transport,expected_key",
    [
        ("credit_tax_reallocation", "mcp_record_api", "connector_schema"),
        ("sales_order_line_alignment", "mcp_record_api", "connector_schema"),
        ("credit_tax_reallocation", None, "schema_version"),  # native profile
        ("sales_adjustment_credit", None, "currency"),  # commercial profile
        ("invoice_sales_adjustment", None, "currency"),
        ("sales_order_source_alignment", None, "currency"),
        ("invoice_tax", None, "tax_item_id"),
        (None, None, "tax_item_id"),
    ],
)
def test_treatment_profile_is_selected_by_transport_before_kind(kind, transport, expected_key):
    p = _proposal(kind, execution_transport=transport)
    if transport == "mcp_record_api":
        p.pop("native_profile")  # the exact shape that crashed the 54-case group
    assert expected_key in treatment_profile(p)


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("sales_order_source_alignment", ("invoice", "22")),
        ("credit_tax_reallocation", ("invoice", "22")),
        ("sales_order_line_alignment", ("invoice", "22")),
        ("sales_adjustment_credit", ("invoice", "900")),  # the credit is created against the invoice lock
        ("invoice_sales_adjustment", ("invoice", "900")),
        ("invoice_tax", ("invoice", "900")),
    ],
)
def test_collision_key_names_the_document_two_corrections_must_not_share(kind, expected):
    assert collision_key(_proposal(kind)) == expected


def test_declared_target_is_used_but_never_over_disagreeing_evidence():
    agreeing = _proposal(
        "credit_tax_reallocation", reconciliation_target={"record_type": "salesorder", "record_id": "12"}
    )
    assert reconciliation_target_id(agreeing) == "12"
    no_edge = _proposal(
        "credit_tax_reallocation", reconciliation_target={"record_type": "salesorder", "record_id": "12"}
    )
    no_edge["support"]["invoice"].pop("createdFrom")
    assert reconciliation_target_id(no_edge) is None  # a declaration never replaces the collected edge
    disagreeing = _proposal(
        "credit_tax_reallocation", reconciliation_target={"record_type": "salesorder", "record_id": "77"}
    )
    assert reconciliation_target_id(disagreeing) is None  # the collected edge says 12: refuse
    invoice = _proposal("invoice_tax", reconciliation_target={"record_type": "salesorder", "record_id": "77"})
    assert reconciliation_target_id(invoice) is None  # createdFrom says 12: refuse


def test_reconciliation_target_rules_per_family_when_nothing_is_declared():
    assert reconciliation_target_id(_proposal("sales_order_source_alignment")) == "900"
    assert reconciliation_target_id(_proposal("sales_order_line_alignment")) == "900"
    assert reconciliation_target_id(_proposal("invoice_tax")) == "12"  # invoice created from the SO
    assert reconciliation_target_id(_proposal("sales_adjustment_credit")) == "12"
    credit = _proposal("credit_tax_reallocation")
    assert reconciliation_target_id(credit) == "12"  # the invoice -> sales-order edge
    credit["support"]["invoice"]["createdFrom"]["id"] = "other"
    assert reconciliation_target_id(credit) is None  # the edge disagrees: refuse, never guess
    credit = _proposal("credit_tax_reallocation", sales_order_id=None)
    assert reconciliation_target_id(credit) is None


@pytest.mark.parametrize(
    "proposal,expected",
    [
        (_proposal("sales_adjustment_credit"), True),
        (_proposal("credit_tax_reallocation"), True),
        ({"kind": None, "record_type": "invoice", "proposed_fields": {"taxRate": "8.45"}}, True),
        ({"kind": "invoice_tax", "record_type": "invoice", "proposed_fields": {"taxRate": "8.45"}}, True),
        ({"kind": "invoice_tax", "record_type": "invoice", "proposed_fields": {"taxRate": "8.45", "memo": "x"}}, False),
        ({"kind": None, "record_type": "creditmemo", "proposed_fields": {"taxRate": "8.45"}}, False),
        ({}, False),
        (None, False),
    ],
)
def test_supports_is_the_registry_membership_rule(proposal, expected):
    assert treatments.supports(proposal) is expected
    assert accounting_recheck.supports(proposal) is expected


def _kind_literal_collections(path):
    """Set, dict-key, list and tuple literals naming two or more registry kinds."""
    tree = ast.parse(path.read_text())
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
            values = node.elts
        elif isinstance(node, ast.Dict):
            values = node.keys
        else:
            continue
        kinds = [v.value for v in values if isinstance(v, ast.Constant) and v.value in REGISTRY]
        if len(kinds) >= 2:
            found.append((node.lineno, sorted(kinds)))
    return found


def test_no_second_definition_of_the_kind_sets_exists_in_the_app_tree():
    offenders = {}
    for path in sorted(APP.rglob("*.py")):
        if path.name == "treatments.py":
            continue
        hits = _kind_literal_collections(path)
        if hits:
            offenders[str(path.relative_to(APP))] = hits
    assert offenders == {}, offenders


async def test_mcp_credit_prepare_declares_its_reconciliation_target(mcp_credit):  # noqa: F811
    """G1(c): the adapter declares the sales order its recheck must describe; readers stop re-deriving it."""
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from tests.test_accounting_release_regressions import corrected

    p, _, _ = mcp_credit
    assert p["reconciliation_target"] == {"record_type": "salesorder", "record_id": str(p["sales_order_id"])}
    now = datetime.now(timezone.utc)
    p, _, report = corrected(mcp_credit, now)
    run = SimpleNamespace(params_json={"verified_at": (now - timedelta(seconds=1)).isoformat()})
    assert accounting_recheck.report_in_scope(run, p, report, now)
    # The declaration never replaces the independently collected edge.
    p["support"]["invoice"].pop("createdFrom")
    assert not accounting_recheck.report_in_scope(run, p, report, now)
    # A stored proposal without the declaration still binds through the edge.
    legacy = {k: v for k, v in p.items() if k != "reconciliation_target"}
    legacy["support"]["invoice"]["createdFrom"] = {"id": p["sales_order_id"]}
    assert accounting_recheck.report_in_scope(run, legacy, report, now)


@pytest.mark.parametrize(
    "kind", ["sales_order_source_alignment", "credit_tax_reallocation", "sales_order_line_alignment"]
)
def test_collision_key_fails_closed_without_the_invoice_id(kind):
    p = _proposal(kind)
    del p["invoice_id"]
    with pytest.raises(KeyError):
        collision_key(p)


def test_treatment_profile_fails_closed_without_a_tax_item():
    p = _proposal("invoice_tax")
    del p["tax_item"]
    with pytest.raises(KeyError):
        treatment_profile(p)


def test_family_of_tolerates_unregistered_kinds():
    assert treatments.family_of({"kind": "tax_reversal"}) is None
    assert treatments.family_of({}) == "invoice_tax"


async def test_group_manifest_rejects_a_member_without_its_lock_document(mcp_credit):  # noqa: F811
    from uuid import uuid4

    from app.services.transaction_ops import accounting_group
    from tests.test_accounting_release_regressions import member

    p, _, _ = mcp_credit
    session_id = uuid4()
    card = accounting_group.build_group_card(
        [member(p, session_id)], {"group_id": "g", "scope": p["scope"]}, str(session_id)
    )
    so = card.model_dump(mode="json")
    del so["accounting_group"]["members"][0]["card"]["accounting_review"]["invoice_id"]
    with pytest.raises(ValueError):
        accounting_group.validate_manifest(so, str(session_id))
