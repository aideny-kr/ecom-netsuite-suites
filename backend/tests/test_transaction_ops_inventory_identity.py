"""Live Framework imports bind lines through exact inventory-unit sets, not SKUs alone."""

from copy import deepcopy

import pytest

from app.services.transaction_ops.normalization import normalize_framework_order, normalize_netsuite_order
from tests.test_transaction_ops_legacy_tax import NOW, compare, config, raw_target
from tests.test_transaction_ops_normalization import evidence


def inventory_case():
    policy = config("line_tax_amount", line_identity_mode="inventory_units")
    raw_source = evidence()
    raw_source["orders"][0]["line_items"][0].update(variant={"sku": "FRAME-1"}, inventory_units=[{"id": "501"}])
    raw = raw_target("line_tax_amount")
    raw["lines"][0].pop("custcol_fw_solidus_line_id")
    raw["lines"][0].update(custcol_fw_inventory_unit_ids="501", custcol_fw_original_ecom_sku="FRAME-1")
    return policy, raw_source, raw


def snapshots(policy, source, target):
    src = normalize_framework_order(source, mapping=policy, account_id="frame.work", subsidiary_id="1")
    dst = normalize_netsuite_order(
        target, mapping=policy, account_id="6738075", observed_at=NOW.isoformat(), source=src
    )
    return src, dst


def test_exact_native_inventory_and_original_sku_establish_line_identity():
    policy, raw_source, raw = inventory_case()
    src, dst = snapshots(policy, raw_source, raw)
    assert src.lines[0].inventory_unit_ids == dst.lines[0].inventory_unit_ids == ("501",)
    assert src.lines[0].sku == dst.lines[0].sku == "FRAME-1"
    assert src.lines[0].key == dst.lines[0].key == "line:11"
    assert src.lines_complete and dst.lines_complete and dst.tax_complete
    assert compare(src, dst).recommended_action == "no_action"


@pytest.mark.parametrize("value", [None, "", "501,501", "0501", "501,secret", "502", 501, True])
def test_unknown_ambiguous_or_different_native_inventory_never_pairs(value):
    policy, raw_source, raw = inventory_case()
    raw["lines"][0]["custcol_fw_inventory_unit_ids"] = value
    src, dst = snapshots(policy, raw_source, raw)
    assert not dst.lines_complete
    assert compare(src, dst).recommended_action == "gather_evidence"


@pytest.mark.parametrize("value", [None, "", "FRAME-2"])
def test_inventory_match_does_not_hide_wrong_or_missing_original_sku(value):
    policy, raw_source, raw = inventory_case()
    raw["lines"][0]["custcol_fw_original_ecom_sku"] = value
    src, dst = snapshots(policy, raw_source, raw)
    assert not dst.lines_complete
    assert compare(src, dst).recommended_action == "gather_evidence"


@pytest.mark.parametrize("value", [None, [], [{"id": "501"}, {"id": "501"}], [{"id": True}], [{"id": "0501"}]])
def test_unknown_or_repeated_source_inventory_is_not_complete(value):
    policy, raw_source, raw = inventory_case()
    raw_source["orders"][0]["line_items"][0]["inventory_units"] = value
    src, dst = snapshots(policy, raw_source, raw)
    assert not src.lines_complete and not dst.lines_complete


def test_partial_inventory_overlap_cannot_merge_or_split_source_lines():
    policy, raw_source, raw = inventory_case()
    raw_source["orders"][0]["line_items"][0]["inventory_units"] = [{"id": "501"}, {"id": "502"}]
    src, dst = snapshots(policy, raw_source, raw)
    assert not dst.lines_complete


def test_inventory_order_is_canonical_and_does_not_change_identity():
    policy, raw_source, raw = inventory_case()
    raw_source["orders"][0]["line_items"][0]["inventory_units"] = [{"id": "502"}, {"id": "501"}]
    raw["lines"][0]["custcol_fw_inventory_unit_ids"] = "501, 502"
    src, dst = snapshots(policy, raw_source, raw)
    assert dst.lines_complete and src.lines[0].inventory_unit_ids == dst.lines[0].inventory_unit_ids == ("501", "502")


def test_same_inventory_unit_on_distinct_source_lines_is_ambiguous():
    policy, raw_source, raw = inventory_case()
    duplicate = deepcopy(raw_source["orders"][0]["line_items"][0])
    duplicate["id"] = "12"
    duplicate["adjustments"][0].update(id="100", adjustable_id="12")
    raw_source["orders"][0]["line_items"].append(duplicate)
    src, dst = snapshots(policy, raw_source, raw)
    assert not src.lines_complete and not dst.lines_complete


def test_native_quantity_difference_remains_visible_after_identity_binding():
    policy, raw_source, raw = inventory_case()
    raw["lines"][0]["quantity"] = "2"
    src, dst = snapshots(policy, raw_source, raw)
    result = compare(src, dst)
    assert any(d.field == "lines.line:11.quantity" for d in result.differences)


@pytest.mark.parametrize("changes", [{"inventory_unit_ids": ("999",)}, {"inventory_unit_ids": ()}, {"sku": "FRAME-2"}])
def test_comparator_retains_identity_proof_even_for_caller_supplied_snapshots(changes):
    policy, raw_source, raw = inventory_case()
    src, dst = snapshots(policy, raw_source, raw)
    dst = dst.model_copy(update={"lines": (dst.lines[0].model_copy(update=changes),)})
    result = compare(src, dst)
    assert result.recommended_action == "human_review"
    assert "line_identity_mismatch" in {finding.code for finding in result.findings}


def test_native_collector_preserves_inventory_ids_required_by_the_live_import():
    from app.services.transaction_ops.netsuite_reader import LINE_FIELDS, _project

    projected = _project({"custcol_fw_inventory_unit_ids": "501,502", "raw_credentials": "secret"}, LINE_FIELDS)
    assert projected == {"custcol_fw_inventory_unit_ids": "501,502"}


@pytest.mark.asyncio
async def test_investigation_requests_private_identity_and_binds_native_lines():
    from unittest.mock import AsyncMock
    from uuid import UUID

    from app.services.transaction_ops.runner import run_investigation
    from tests.test_transaction_ops_runner import NOW as RUN_NOW
    from tests.test_transaction_ops_runner import REF, State

    policy, raw_source, raw = inventory_case()
    raw_source["read_at"] = RUN_NOW.isoformat()
    state = State()
    state.run.config_snapshot["mapping_json"] = policy.model_dump(mode="json")
    source = AsyncMock(return_value=raw_source)
    targets = {
        "provider": "netsuite",
        "orders": [raw],
        "lookup": {"complete": True},
        "observed_at": RUN_NOW.isoformat(),
        "scope": {"account_id": "6738075", "subsidiary_id": "1"},
    }
    result = await run_investigation(
        None,
        state.tenant,
        state.run_id,
        _state=state,
        _source_reader=source,
        _target_reader=AsyncMock(return_value=targets),
        _enabled=AsyncMock(return_value=True),
        _clock=lambda: RUN_NOW,
    )
    assert result["termination_reason"] == "done"
    source.assert_awaited_once_with(
        None, state.tenant, UUID(state.run.config_snapshot["source_step_id"]), REF, include_sync_data=True
    )
    report = state.reports[REF]
    assert report["comparison"]["recommended_action"] == "no_action"
    assert report["targets"][0]["lines"][0]["key"] == "line:11"


@pytest.mark.parametrize(
    "changes",
    [{"order_reference": "R999999999"}, {"system": "other"}, {"account_id": "another-store"}, {"subsidiary_id": "999"}],
)
def test_inventory_binding_requires_the_same_authoritative_source_scope(changes):
    policy, raw_source, raw = inventory_case()
    src, _ = snapshots(policy, raw_source, raw)
    dst = normalize_netsuite_order(
        raw, mapping=policy, account_id="6738075", observed_at=NOW.isoformat(), source=src.model_copy(update=changes)
    )
    assert not dst.lines_complete
