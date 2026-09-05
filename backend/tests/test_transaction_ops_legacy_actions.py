"""Legacy corrections bind native tax fields as well as copied source VAT."""

from copy import deepcopy

import pytest

from app.services.transaction_ops import netsuite_actions as mod
from app.services.transaction_ops.normalization import NetSuiteLegacyTaxMapping
from tests.test_transaction_ops_netsuite_actions import source, target


def profile(mode="aggregate_header", **changes):
    return NetSuiteLegacyTaxMapping.model_validate(
        {
            "schema_version": 1,
            "mode": mode,
            "account_id": "1234567_SB1",
            "subsidiary_id": "3",
            "tax_code_id": "610",
            **changes,
        }
    )


def legacy_source(mode="aggregate_header", **changes):
    values = {"shipping": "0", "total": "120"} if mode == "aggregate_header" else {}
    return source(**(values | changes))


def legacy_target(mode="aggregate_header"):
    raw = target()
    raw["tax_details"] = None
    raw["header"].update(custbody_fw_solidus_tax_amount="18", shippingTax1Rate="0", shippingTax2Rate="0")
    raw["lines"][0]["isTaxable"] = True
    if mode == "aggregate_header":
        raw["header"].update(
            taxItem={"id": "610"},
            taxRate="19.5652174",
            isTaxable=True,
            shippingCost="0",
            total="108",
            custbody_fw_solidus_order_total="108",
        )
        raw["lines"][0].pop("taxCode")
        raw["lines"][0].pop("taxRate1")
    else:
        raw["lines"][0].update(tax1Amt="18", taxRate1="20.001")
    return raw


def prepare(raw=None, src=None, mode="aggregate_header", **kwargs):
    kwargs.setdefault("tax_rounding", "half_up")
    return mod.prepare_correction(
        raw or legacy_target(mode),
        src or legacy_source(mode),
        legacy_tax=profile(mode),
        account_id="1234567-sb1",
        **kwargs,
    )


def test_aggregate_correction_binds_header_rate_tax_item_custom_amount_and_taxability():
    plan = prepare()
    assert plan.before_json["tax_profile"] == {"mode": "aggregate_header", "tax_code_id": "610"}
    assert plan.before_json["taxitem"] == "610"
    assert plan.before_json["taxrate"] == "19.5652174"
    assert plan.before_json["istaxable"] is True
    line = plan.before_json["lines"][0]
    assert line["istaxable"] is True and "taxcode" not in line and "taxrate1" not in line
    assert plan.after_json["body_changes"] == {
        "custbody_fw_solidus_order_total": "120",
        "custbody_fw_solidus_tax_amount": "20",
        "taxrate": "20",
    }
    assert "taxrate1" not in plan.after_json["line_changes"][0]["fields"]


def test_line_tax_amount_correction_sets_native_and_custom_vat_without_changing_native_rate():
    plan = prepare(mode="line_tax_amount")
    line = plan.before_json["lines"][0]
    assert line["tax1amt"] == "18" and line["taxrate1"] == "20.001"
    fields = plan.after_json["line_changes"][0]["fields"]
    assert fields == {"rate": "50", "amount": "100", "custcol_fw_vat_amount": "20", "tax1amt": "20"}
    assert plan.after_json["body_changes"]["custbody_fw_solidus_tax_amount"] == "20"
    assert "taxrate" not in plan.after_json["body_changes"]


@pytest.mark.parametrize("mode", ["aggregate_header", "line_tax_amount"])
def test_multiple_components_are_validated_then_allocated_to_the_exact_native_code(mode):
    src = legacy_source(
        mode,
        tax_details=[
            {
                "key": "line:11:tax:610:source_rate:7:adjustment:1",
                "allocation_key": "line:11:tax:610",
                "basis": "100",
                "rate": "0.05",
                "amount": "5",
                "rounding": "half_up",
            },
            {
                "key": "line:11:tax:610:source_rate:8:adjustment:2",
                "allocation_key": "line:11:tax:610",
                "basis": "100",
                "rate": "0.15",
                "amount": "15",
                "rounding": "half_up",
            },
        ],
    )
    assert prepare(src=src, mode=mode).after_json["expected_totals"]["taxtotal"] == "20"
    src["tax_details"][1]["rate"] = "0.16"
    with pytest.raises(mod.NetSuiteActionError, match="source_tax_inconsistent"):
        prepare(src=src, mode=mode)


@pytest.mark.parametrize("mode", ["aggregate_header", "line_tax_amount"])
@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["header"].update(custbody_fw_solidus_tax_amount="19"),
        lambda r: r["header"].update(shippingCost="2", shippingTax1Rate="20"),
        lambda r: r["lines"][0].update(isTaxable=False),
        lambda r: r["lines"][0].pop("isTaxable"),
        lambda r: r["header"].update(subsidiary={"id": "4"}),
    ],
)
def test_profile_requires_fresh_complete_scope_and_unallocated_tax_proof(mode, mutate):
    raw = legacy_target(mode)
    mutate(raw)
    with pytest.raises(mod.NetSuiteActionError):
        prepare(raw=raw, mode=mode)


def test_profile_cannot_be_applied_to_another_account_or_suitetax_record():
    with pytest.raises(mod.NetSuiteActionError, match="scope"):
        mod.prepare_correction(legacy_target(), source(), legacy_tax=profile(), account_id="9999999")
    with pytest.raises(mod.NetSuiteActionError, match="legacy"):
        prepare(raw=target())


def test_line_profile_cannot_hide_disagreement_between_native_and_custom_tax():
    raw = legacy_target("line_tax_amount")
    raw["lines"][0]["tax1Amt"] = "19"
    with pytest.raises(mod.NetSuiteActionError, match="tax"):
        prepare(raw=raw, mode="line_tax_amount")


def test_effective_header_formula_is_explicit_and_never_changes_currency_or_fx():
    src, raw = legacy_source(currency="USD"), legacy_target()
    raw["currency_metadata"]["symbol"] = "USD"
    plan = prepare(raw, src)
    assert plan.after_json["body_changes"]["taxrate"] == "20"
    assert not {"currency", "exchangerate", "entity", "subsidiary"} & plan.after_json["body_changes"].keys()


@pytest.mark.parametrize("mode", ["aggregate_header", "line_tax_amount"])
def test_zero_tax_correction_clears_the_native_legacy_rate_or_amount(mode):
    src = legacy_source(
        mode,
        total="100" if mode == "aggregate_header" else "102",
        tax="0",
        tax_details=[],
        lines=[{"key": "line:11", "quantity": "2", "net": "100", "tax": "0"}],
    )
    plan = prepare(src=src, mode=mode)
    if mode == "aggregate_header":
        assert plan.after_json["body_changes"]["taxrate"] == "0"
    else:
        assert plan.after_json["line_changes"][0]["fields"]["tax1amt"] == "0"


def test_planner_binds_native_profile_instead_of_using_generic_line_rate_write():
    from app.services.transaction_ops import planner
    from app.services.transaction_ops.normalization import TransactionMapping
    from app.services.transaction_ops.runner import build_report
    from tests.test_transaction_ops_planner import planning_case

    case = planning_case()
    policy = profile(account_id=case.config.netsuite_account_id)
    case.config.mapping_json["netsuite_legacy_tax"] = policy.model_dump(mode="json")
    case.config.mapping_json["netsuite_tax_rounding"] = "half_up"
    raw = case.targets["orders"][0]
    raw["tax_details"] = None
    raw["header"].update(
        isTaxable=True,
        taxItem={"id": "610"},
        taxRate="0",
        custbody_fw_solidus_tax_amount="0",
        shippingTax1Rate="0",
        shippingTax2Rate="0",
    )
    raw["lines"][0].update(isTaxable=True)
    raw["lines"][0].pop("taxCode")
    raw["lines"][0].pop("taxRate1")
    case.report = build_report(
        case.source,
        case.targets,
        {"netsuite_account_id": case.config.netsuite_account_id, "subsidiary_id": "3", "record_type": "salesorder"},
        TransactionMapping.model_validate(case.config.mapping_json),
        now=case.now,
    )
    plan = mod.prepare_correction(
        raw,
        case.report["source"],
        now=case.now,
        legacy_tax=policy,
        account_id=case.config.netsuite_account_id,
        tax_rounding="half_up",
    )
    case.guard["snapshot"] = plan.before_json
    result = planner.plan_proposal(case.report, case.targets, case.config, guard=case.guard, now=case.now)
    assert result.before_json == plan.before_json and result.after_json == plan.after_json
    assert "taxcode" not in result.before_json["lines"][0]
    assert result.evidence_json["native_tax_rounding"] == "half_up"
    case.config.mapping_json["netsuite_tax_rounding"] = "half_even"
    other = planner.plan_proposal(case.report, case.targets, case.config, guard=case.guard, now=case.now)
    assert other.before_json == result.before_json and other.after_json == result.after_json
    assert other.evidence_fingerprint != result.evidence_fingerprint


async def test_guard_read_requests_and_requires_the_exact_configured_profile():
    from types import SimpleNamespace

    import httpx

    from app.services.transaction_ops import netsuite_transport as transport

    policy = profile()
    cfg = SimpleNamespace(
        netsuite_account_id="1234567_SB1",
        subsidiary_id="3",
        mapping_json={"reference_field": "tranid", "netsuite_legacy_tax": policy.model_dump(mode="json")},
    )
    requested = []

    async def handle(request):
        requested.append(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "success": True,
                "schema_version": 1,
                "account_id": "1234567_SB1",
                "actions_enabled": True,
                "snapshot": {
                    "record_id": "63",
                    "subsidiary": "3",
                    "reference_field": "tranid",
                    "tax_profile": {"mode": "line_tax_amount", "tax_code_id": "610"},
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(mod.NetSuiteActionError, match="snapshot"):
            await transport._snapshot(
                client,
                "https://1234567-sb1.restlets.api.netsuite.com/app/site/hosting/restlet.nl",
                "fixture",
                cfg,
                "63",
            )
    assert requested[0]["tax_mode"] == "aggregate_header" and requested[0]["tax_code_id"] == "610"


def test_tiny_positive_tax_cannot_be_rounded_into_zero_native_header_rate():
    src = source(
        total="10000000000.01",
        subtotal="10000000000",
        tax="0.01",
        shipping="0",
        lines=[{"key": "line:11", "quantity": "2", "net": "10000000000", "tax": "0.01"}],
        tax_details=[
            {
                "key": "line:11:tax:610",
                "basis": "10000000000",
                "rate": "0.000000000001",
                "amount": "0.01",
                "rounding": "half_up",
            }
        ],
    )
    with pytest.raises(mod.NetSuiteActionError, match="unsupported_header_tax_rate"):
        prepare(src=src)


@pytest.mark.parametrize("mode", ["aggregate_header", "line_tax_amount"])
def test_independent_verification_requires_native_tax_fields_even_when_all_amounts_match(mode):
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from app.schemas.transaction_ops import TransactionLookup, TransactionSnapshot
    from app.services.transaction_ops import executor
    from app.services.transaction_ops.comparison import compare_transactions
    from app.services.transaction_ops.normalization import TransactionMapping, normalize_netsuite_order

    now = datetime.now(timezone.utc)
    src = TransactionSnapshot.model_validate(legacy_source(mode))
    plan = prepare(src=src, mode=mode)
    raw = legacy_target(mode)
    raw["header"].update(
        total=str(src.total),
        subtotal="100",
        taxTotal="20",
        custbody_fw_solidus_tax_amount="20",
        custbody_fw_solidus_order_total=str(src.total),
    )
    raw["lines"][0].update(rate="50", amount="100", custcol_fw_vat_amount="20")
    if mode == "line_tax_amount":
        raw["lines"][0]["tax1Amt"] = "20"
    else:
        raw["header"]["taxRate"] = "20"
    dst = normalize_netsuite_order(
        raw,
        mapping=TransactionMapping(reference_field="tranid", netsuite_legacy_tax=profile(mode)),
        account_id="1234567-sb1",
        observed_at=now.isoformat(),
    )
    lookup = TransactionLookup(
        source_system="framework",
        source_account_id="frame.work",
        source_record_id=src.record_id,
        order_reference=src.order_reference,
        target_account_id=dst.account_id,
        target_subsidiary_id="3",
        target_record_type="salesorder",
        complete=True,
        authoritative=True,
        observed_at=now,
    )
    report = {
        "source": src.model_dump(mode="json"),
        "targets": [dst.model_dump(mode="json")],
        "comparison": compare_transactions(src, [dst], lookup, now=datetime.now(timezone.utc)).model_dump(mode="json"),
    }
    assert report["comparison"]["recommended_action"] == "no_action"
    proposal = SimpleNamespace(
        action="correct_amounts",
        target_record_id="63",
        before_json=plan.before_json,
        after_json=plan.after_json,
        evidence_json={"schema_version": 1, "report": {"source": src.model_dump(mode="json")}},
    )
    observed = deepcopy(plan.before_json)
    observed.update(plan.after_json["body_changes"])
    observed.update(plan.after_json["expected_totals"])
    for change in plan.after_json["line_changes"]:
        next(line for line in observed["lines"] if line["line"] == change["line"]).update(change["fields"])
    guard = {"snapshot": observed}
    assert executor.verify_outcome(proposal, report, guard=guard) is not None
    if mode == "aggregate_header":
        observed["taxrate"] = "19"
    else:
        observed["lines"][0]["taxrate1"] = "20"
    assert executor.verify_outcome(proposal, report, guard=guard) is None


@pytest.mark.parametrize("side", ["before", "after"])
def test_aggregate_write_requires_proven_zero_shipping_on_both_sides(side):
    raw = legacy_target()
    raw["header"].update(shippingCost="2" if side == "before" else "0")
    src = source(shipping="2" if side == "after" else "0", total="122" if side == "after" else "120")
    with pytest.raises(mod.NetSuiteActionError, match="unsupported_aggregate_shipping"):
        prepare(raw, src)


def test_included_usd_assessment_uses_native_net_line_basis_for_header_rate():
    raw = legacy_target()
    raw["header"].update(shippingCost="0", total="108", custbody_fw_solidus_order_total="108")
    raw["currency_metadata"]["symbol"] = "USD"
    src = source(currency="USD", shipping="0", total="120")
    src["tax_details"][0].update(included_gross_basis="120", included_rate_total="0.2")
    plan = prepare(raw, src)
    assert plan.after_json["body_changes"]["taxrate"] == "20"


def test_aggregate_requires_explicit_native_tax_rounding_policy():
    with pytest.raises(mod.NetSuiteActionError, match="native_tax_rounding_unproven"):
        mod.prepare_correction(legacy_target(), legacy_source(), legacy_tax=profile(), account_id="1234567-sb1")


def test_header_rate_precision_must_reproduce_the_exact_currency_tax_amount():
    src = legacy_source(
        total="10000000100.06",
        subtotal="10000000000",
        tax="100.06",
        lines=[{"key": "line:11", "quantity": "2", "net": "10000000000", "tax": "100.06"}],
        tax_details=[
            {
                "key": "line:11:tax:610",
                "basis": "10000000000",
                "rate": "0.000000010006",
                "amount": "100.06",
                "rounding": "half_up",
            }
        ],
    )
    with pytest.raises(mod.NetSuiteActionError, match="unsupported_header_tax_precision"):
        prepare(src=src)
