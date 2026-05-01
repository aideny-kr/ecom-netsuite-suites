"""Pricing agent tool executors — currency conversion and config read."""

from __future__ import annotations

import io
import uuid
from decimal import Decimal

from openpyxl import load_workbook

from app.models.pricing_conversion_log import PricingConversionLog
from app.schemas.pricing import PricingInput, TenantPricingConfig
from app.services.pricing_config_service import get_config, upsert_config
from app.services.pricing_engine import PricingEngine
from app.services.task_file_service import TaskFileService
from app.services.template_filler import TemplateFiller

_engine = PricingEngine()
_filler = TemplateFiller()
_file_svc = TaskFileService()


def _build_preview(results, max_rows: int = 10) -> list[dict]:
    """Frontend-friendly preview: flat dict per row with sorted currency keys."""
    preview: list[dict] = []
    for r in results[:max_rows]:
        row = {"SKU": r.sku, "USD": float(r.usd_price)}
        for code, cr in sorted(r.results.items()):
            row[code] = float(cr.final_price)
        preview.append(row)
    return preview


def _seed_pricing_state(
    *,
    items: list[PricingInput],
    pricing_config: TenantPricingConfig,
    excel_file_id: str,
    netsuite_csv_file_id: str,
    row_count: int,
) -> dict:
    """Build the initial pricing_state payload that will be cached for follow-ups.

    Holds only what pricing_revise / pricing_to_sheets need: the seed inputs,
    effective inputs/currencies/overrides (all empty on the seed run), and
    pointers to the saved Excel + NetSuite CSV files. Row data is NOT cached
    — pricing_to_sheets re-parses the Excel file on demand.
    """
    seed_items = [
        {"sku": it.sku, "usd_price": str(it.usd_price), "item_name": it.item_name}
        for it in items
    ]
    currency_codes = list(pricing_config.currencies.keys())
    header_columns = ["SKU", "Item Name", "USD", *sorted(currency_codes)]
    return {
        "seed_items": seed_items,
        "effective_items": [dict(s) for s in seed_items],
        "effective_currencies": currency_codes,
        "effective_fx_overrides": {},
        "effective_vat_overrides": {},
        "effective_rounding_overrides": {},
        "effective_uplift_by_currency": {},
        "applied_overrides_log": [],
        "excel_file_id": excel_file_id,
        "netsuite_csv_file_id": netsuite_csv_file_id,
        "header_columns": header_columns,
        "row_count": row_count,
    }


async def pricing_convert_execute(params: dict, context: dict, **kwargs) -> dict:
    """Convert prices in uploaded Excel using tenant FX rates."""
    if not context or not context.get("db") or not context.get("tenant_id"):
        return {"error": True, "message": "Missing context — tenant_id and db are required."}
    db = context["db"]
    tenant_id = context["tenant_id"]
    user_id = context.get("actor_id") or context.get("user_id")

    # 1. Load config
    config_row = await get_config(db, tenant_id)
    if not config_row:
        return {"error": True, "message": "No pricing configuration found. Set up FX rates in Settings."}
    pricing_config = TenantPricingConfig(**config_row.config)

    # 2. Load file
    file_id = params.get("file_id")
    if not file_id:
        return {"error": True, "message": "file_id is required"}
    try:
        task_file, content = await _file_svc.get_file(db, tenant_id, uuid.UUID(file_id))
    except ValueError:
        return {"error": True, "message": f"File not found: {file_id}"}

    # 3. Parse Excel
    wb = load_workbook(io.BytesIO(content))
    mapping = _filler.detect_columns(wb)
    ws = wb.active
    items = []
    for row in range(2, ws.max_row + 1):
        sku_val = ws.cell(row=row, column=mapping.sku_col + 1).value
        price_val = ws.cell(row=row, column=mapping.price_col + 1).value
        if not sku_val or not price_val:
            continue
        items.append(PricingInput(sku=str(sku_val).strip(), usd_price=Decimal(str(price_val))))
    if not items:
        return {"error": True, "message": "No valid rows found. Need SKU and USD Price columns."}

    # 4. Convert
    results = _engine.convert_batch(items, pricing_config)

    # 5. Generate output
    output_files = {}
    if mapping.currency_cols:
        _filler.fill(wb, results, mapping)
        buf = io.BytesIO()
        wb.save(buf)
        output_content = buf.getvalue()
    else:
        out_wb = _filler.generate_default_output(results)
        buf = io.BytesIO()
        out_wb.save(buf)
        output_content = buf.getvalue()

    excel_file = await _file_svc.save_output(
        db=db,
        tenant_id=tenant_id,
        user_id=user_id,
        filename=f"pricing-{task_file.filename}",
        content=output_content,
    )
    output_files["excel"] = str(excel_file.id)

    # 6. NetSuite CSV (always generated)
    csv_str = _filler.generate_netsuite_csv(results)
    csv_file = await _file_svc.save_output(
        db=db,
        tenant_id=tenant_id,
        user_id=user_id,
        filename=f"netsuite-import-{task_file.filename.rsplit('.', 1)[0]}.csv",
        content=csv_str.encode("utf-8"),
    )
    output_files["netsuite_csv"] = str(csv_file.id)

    # 7. Audit log

    db.add(
        PricingConversionLog(
            tenant_id=tenant_id,
            user_id=user_id,
            input_file_id=uuid.UUID(file_id),
            output_file_id=excel_file.id,
            sku_count=len(items),
            currency_count=len(pricing_config.currencies),
            config_snapshot=pricing_config.model_dump(mode="json"),
        )
    )

    pricing_state = _seed_pricing_state(
        items=items,
        pricing_config=pricing_config,
        excel_file_id=str(excel_file.id),
        netsuite_csv_file_id=str(csv_file.id),
        row_count=len(items),
    )

    return {
        "success": True,
        "sku_count": len(items),
        "currency_count": len(pricing_config.currencies),
        "output_files": output_files,
        "preview": _build_preview(results),
        "pricing_state": pricing_state,
        "template_mode": bool(mapping.currency_cols),
    }


async def pricing_export_execute(params: dict, context: dict, **kwargs) -> dict:
    """Export computed prices to downloadable Excel from inline data (no upload required)."""
    if not context or not context.get("db") or not context.get("tenant_id"):
        return {"error": True, "message": "Missing context — tenant_id and db are required."}
    db = context["db"]
    tenant_id = context["tenant_id"]
    user_id = context.get("actor_id") or context.get("user_id")

    # 1. Load config
    config_row = await get_config(db, tenant_id)
    if not config_row:
        return {"error": True, "message": "No pricing configuration found. Set up FX rates in Settings."}
    pricing_config = TenantPricingConfig(**config_row.config)

    # 2. Parse inline items
    raw_items = params.get("items")
    if not raw_items or not isinstance(raw_items, list):
        return {"error": True, "message": "items is required (list of {sku, usd_price, item_name?})"}
    items = []
    for raw in raw_items:
        sku = raw.get("sku")
        price = raw.get("usd_price")
        if not sku or price is None:
            continue
        items.append(
            PricingInput(
                sku=str(sku).strip(),
                item_name=raw.get("item_name"),
                usd_price=Decimal(str(price)),
            )
        )
    if not items:
        return {"error": True, "message": "No valid items. Each needs at least sku and usd_price."}

    # 3. Convert
    results = _engine.convert_batch(items, pricing_config)

    # 4. Generate output Excel (default 3-sheet workbook)
    out_wb = _filler.generate_default_output(results)
    buf = io.BytesIO()
    out_wb.save(buf)
    output_content = buf.getvalue()

    output_files = {}
    excel_file = await _file_svc.save_output(
        db=db,
        tenant_id=tenant_id,
        user_id=user_id,
        filename=f"pricing-export-{len(items)}-skus.xlsx",
        content=output_content,
    )
    output_files["excel"] = str(excel_file.id)

    # 5. NetSuite CSV (always generated)
    csv_str = _filler.generate_netsuite_csv(results)
    csv_file = await _file_svc.save_output(
        db=db,
        tenant_id=tenant_id,
        user_id=user_id,
        filename=f"netsuite-import-{len(items)}-skus.csv",
        content=csv_str.encode("utf-8"),
    )
    output_files["netsuite_csv"] = str(csv_file.id)

    # 6. Audit log

    db.add(
        PricingConversionLog(
            tenant_id=tenant_id,
            user_id=user_id,
            input_file_id=None,
            output_file_id=excel_file.id,
            sku_count=len(items),
            currency_count=len(pricing_config.currencies),
            config_snapshot=pricing_config.model_dump(mode="json"),
        )
    )

    pricing_state = _seed_pricing_state(
        items=items,
        pricing_config=pricing_config,
        excel_file_id=str(excel_file.id),
        netsuite_csv_file_id=str(csv_file.id),
        row_count=len(items),
    )

    return {
        "success": True,
        "sku_count": len(items),
        "currency_count": len(pricing_config.currencies),
        "output_files": output_files,
        "preview": _build_preview(results),
        "pricing_state": pricing_state,
        "template_mode": False,
    }


async def pricing_config_read_execute(params: dict, context: dict, **kwargs) -> dict:
    """Read current tenant pricing configuration."""
    if not context or not context.get("db") or not context.get("tenant_id"):
        return {"error": True, "message": "Missing context — tenant_id and db are required."}
    db = context["db"]
    tenant_id = context["tenant_id"]
    config_row = await get_config(db, tenant_id)
    if not config_row:
        return {"error": True, "message": "No pricing configuration set."}
    config = TenantPricingConfig(**config_row.config)
    currencies = {}
    for code, cc in config.currencies.items():
        currencies[code] = {
            "fx_rate": float(cc.fx_rate),
            "tier": cc.tier,
            "vat_rate": float(cc.vat_rate) if cc.vat_rate else None,
            "rounding_rule": cc.rounding_rule,
        }
    return {
        "success": True,
        "base_currency": config.base_currency,
        "eur_fx_rate": float(config.eur_fx_rate),
        "currency_count": len(currencies),
        "currencies": currencies,
    }


async def pricing_config_update_execute(params: dict, context: dict, **kwargs) -> dict:
    """Update tenant pricing configuration — FX rates, VAT, rounding rules.

    Accepts partial updates: only the fields provided will be changed.
    """
    if not context or not context.get("db") or not context.get("tenant_id"):
        return {"error": True, "message": "Missing context — tenant_id and db are required."}

    db = context["db"]
    tenant_id = context["tenant_id"]
    user_id = context.get("user_id")

    # Read current config
    config_row = await get_config(db, tenant_id)
    if not config_row:
        return {"error": True, "message": "No pricing configuration exists. Create one in Settings first."}

    current_config = dict(config_row.config)

    # Apply updates
    updates = params.get("updates", {})
    if not updates:
        return {"error": True, "message": "No updates provided. Pass 'updates' with fields to change."}

    # Update EUR FX rate
    if "eur_fx_rate" in updates:
        current_config["eur_fx_rate"] = float(updates["eur_fx_rate"])

    # Update individual currency configs
    if "currencies" in updates:
        existing_currencies = current_config.get("currencies", {})
        for code, changes in updates["currencies"].items():
            code_upper = code.upper()
            if code_upper not in existing_currencies:
                existing_currencies[code_upper] = {}
            for field in ("fx_rate", "vat_rate", "rounding_rule", "tier"):
                if field in changes:
                    existing_currencies[code_upper][field] = changes[field]
        current_config["currencies"] = existing_currencies

    # Save
    await upsert_config(db, tenant_id, current_config, user_id)
    await db.commit()

    return {
        "success": True,
        "message": f"Pricing config updated: {list(updates.keys())}",
        "updated_fields": list(updates.keys()),
    }
