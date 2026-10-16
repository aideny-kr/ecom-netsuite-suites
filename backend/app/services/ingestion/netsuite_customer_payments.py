"""Mirror proven invoice applications, never infer a payment from an amount/memo.

One canonical row per payment/order allocation. Re-reading a payment invalidates
its old allocations before replacing them, so voids and reapplications cannot
leave a reusable match behind. This service never writes to NetSuite.
"""

import hashlib
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.core.database import set_tenant_context
from app.models.canonical import NetsuitePosting
from app.services.netsuite_client import execute_suiteql_via_rest

VERSION = 1


def payment_query(start, end):
    lower, upper = start.isoformat(), end.isoformat()
    return f"""SELECT DISTINCT p.id AS payment_id,p.type AS payment_type,p.tranid,
      TO_CHAR(p.trandate,'YYYY-MM-DD') AS transaction_date,p.foreigntotal AS payment_total,
      p.posting,p.voided,p.currency AS currency_id,c.symbol AS currency,
      p.exchangerate AS exchange_rate,bc.symbol AS base_currency,
      pl.subsidiary,p.entity AS payment_customer,i.id AS invoice_id,
      i.currency AS invoice_currency,il.subsidiary AS invoice_subsidiary,
      i.entity AS invoice_customer,i.posting AS invoice_posting,i.voided AS invoice_voided,
      so.id AS order_id,so.type AS order_type,so.tranid AS order_reference,
      so.currency AS order_currency,sl.subsidiary AS order_subsidiary,
      so.entity AS order_customer,l.previousline,l.nextline,l.linktype,l.foreignamount,
      CASE WHEN EXISTS(SELECT x.previousdoc FROM nexttransactionlink x
        JOIN transaction parent ON parent.id=x.previousdoc
        WHERE x.nextdoc=i.id AND parent.type='SalesOrd' AND parent.id<>so.id)
        THEN 'T' ELSE 'F' END AS shared_invoice
      FROM transaction p JOIN transactionline pl ON pl.transaction=p.id AND pl.mainline='T'
      JOIN currency c ON c.id=p.currency
      JOIN subsidiary sub ON sub.id=pl.subsidiary JOIN currency bc ON bc.id=sub.currency
      LEFT JOIN nexttransactionlinelink l ON l.nextdoc=p.id AND l.linktype='Payment'
      LEFT JOIN transaction i ON i.id=l.previousdoc AND i.type='CustInvc'
      LEFT JOIN transactionline il ON il.transaction=i.id AND il.mainline='T'
      LEFT JOIN transaction so ON so.id=il.createdfrom
      LEFT JOIN transactionline sl ON sl.transaction=so.id AND sl.mainline='T'
      WHERE p.type='CustPymt' AND (
        p.trandate BETWEEN TO_DATE('{lower}','YYYY-MM-DD') AND TO_DATE('{upper}','YYYY-MM-DD')
        OR p.lastmodifieddate BETWEEN TO_DATE('{lower}','YYYY-MM-DD') AND TO_DATE('{upper}','YYYY-MM-DD')+1
        OR EXISTS(SELECT changed.nextdoc FROM nexttransactionlinelink changed
          JOIN transaction inv ON inv.id=changed.previousdoc
          WHERE changed.nextdoc=p.id AND inv.lastmodifieddate BETWEEN
            TO_DATE('{lower}','YYYY-MM-DD') AND TO_DATE('{upper}','YYYY-MM-DD')+1))
      ORDER BY p.id,i.id,l.previousline,l.nextline"""


def allocations(rows, pattern=None):
    from app.services.reconciliation.order_ref import extract_order_ref

    grouped = defaultdict(list)
    for row in rows:
        pid = str(row.get("payment_id", ""))
        if not pid.isdigit() or row.get("payment_type") != "CustPymt":
            raise ValueError("invalid_customer_payment_identity")
        grouped[pid].append(row)
    output = {}
    for pid, items in grouped.items():
        by_order, seen, total = {}, set(), Decimal(0)
        try:
            first = items[0]
            payment = Decimal(first["payment_total"])
            rate = Decimal(first["exchange_rate"])
            if (
                not payment.is_finite()
                or payment <= 0
                or not rate.is_finite()
                or rate <= 0
                or not first.get("base_currency")
            ):
                raise ValueError
            for row in items:
                if (
                    any(
                        row.get(k) != first.get(k)
                        for k in (
                            "payment_total",
                            "posting",
                            "voided",
                            "currency_id",
                            "currency",
                            "subsidiary",
                            "payment_customer",
                            "exchange_rate",
                            "base_currency",
                        )
                    )
                    or row["posting"] != "T"
                    or row["voided"] != "F"
                ):
                    raise ValueError
                # No application is valid absence, not a fabricated zero payment.
                if row.get("invoice_id") is None:
                    continue
                if (
                    row.get("invoice_posting"),
                    row.get("invoice_voided"),
                    row.get("order_type"),
                    row.get("shared_invoice"),
                ) != ("T", "F", "SalesOrd", "F"):
                    raise ValueError
                if not row.get("currency") or len(row["currency"]) != 3 or not row["currency"].isupper():
                    raise ValueError
                if any(row.get(k) != row["currency_id"] for k in ("invoice_currency", "order_currency")):
                    raise ValueError
                if not row.get("subsidiary") or any(
                    row.get(k) != row["subsidiary"] for k in ("invoice_subsidiary", "order_subsidiary")
                ):
                    raise ValueError
                if not row.get("payment_customer") or any(
                    row.get(k) != row["payment_customer"] for k in ("invoice_customer", "order_customer")
                ):
                    raise ValueError
                ref = extract_order_ref(row.get("order_reference"), pattern)
                if ref != row.get("order_reference") or not str(row.get("order_id", "")).isdigit():
                    raise ValueError
                key = (row["invoice_id"], row["previousline"], row["nextline"])
                amount = Decimal(row["foreignamount"])
                if key in seen or not amount.is_finite() or amount <= 0 or row.get("linktype") != "Payment":
                    raise ValueError
                seen.add(key)
                total += amount
                order = by_order.setdefault(row["order_id"], {"row": row, "amount": Decimal(0), "invoices": []})
                order["amount"] += amount
                order["invoices"].append({"id": row["invoice_id"], "amount": str(amount)})
            if total > payment:
                raise ValueError
            output[pid] = list(by_order.values())
        except (ValueError, KeyError, TypeError, ArithmeticError):
            output[pid] = []  # Invalidate old evidence; never match an ambiguous allocation.
    return output


async def sync_payments(db, tenant_id, connection_id, token, account_id, start, end, *, pattern=None):
    observed_at = datetime.now(timezone.utc)
    data = await execute_suiteql_via_rest(
        access_token=token, account_id=account_id, query=payment_query(start, end), limit=50000, paginate=True
    )
    rows = [dict(zip(data.get("columns", []), r)) if isinstance(r, list) else r for r in data.get("rows", [])]
    # The REST helper's finite cap must not look like complete application proof.
    if data.get("truncated") is True or len(rows) >= 50000:
        raise ValueError("customer_payment_read_limit")
    groups = allocations(rows, pattern)
    count = 0
    for pid, values in groups.items():
        await set_tenant_context(db, tenant_id)
        # Serialize even the first insertion. An older response must not
        # overwrite a newer refresh that completed while its HTTP read waited.
        lock_key = int.from_bytes(
            hashlib.sha256(f"{tenant_id}:{connection_id}:payment:{pid}".encode()).digest()[:8], "big", signed=True
        )
        await db.execute(select(func.pg_advisory_xact_lock(lock_key)))
        old = (
            await db.scalars(
                select(NetsuitePosting)
                .where(
                    NetsuitePosting.tenant_id == tenant_id,
                    NetsuitePosting.record_type == "customerpayment",
                    NetsuitePosting.netsuite_internal_id == pid,
                    NetsuitePosting.raw_data["connection_id"].as_string() == str(connection_id),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
        if any(
            datetime.fromisoformat(item.raw_data["observed_at"]) > observed_at
            for item in old
            if (item.raw_data or {}).get("observed_at")
        ):
            await db.commit()
            continue
        for item in old:
            item.raw_data = {
                **(item.raw_data or {}),
                "application_verified": False,
                "observed_at": observed_at.isoformat(),
            }
            item.updated_at = observed_at
        await db.flush()
        for value in values:
            row = value["row"]
            payload = dict(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                dedupe_key=f"netsuite:payment:{connection_id}:{pid}:{row['order_id']}",
                source="netsuite",
                source_id=pid,
                netsuite_internal_id=pid,
                record_type="customerpayment",
                subsidiary_id=row["subsidiary"],
                transaction_date=datetime.fromisoformat(row["transaction_date"]).date(),
                amount=value["amount"] * Decimal(row["exchange_rate"]),
                currency=row["base_currency"],
                transaction_currency=row["currency"],
                foreign_amount=value["amount"],
                exchange_rate=Decimal(row["exchange_rate"]),
                related_payout_id=row["order_reference"],
                raw_data={
                    "application_version": VERSION,
                    "application_verified": True,
                    "connection_id": str(connection_id),
                    "account_id": account_id,
                    "payment_id": pid,
                    "invoice_applications": value["invoices"],
                    "order_id": row["order_id"],
                    "document_number": row["tranid"],
                    "observed_at": observed_at.isoformat(),
                },
                updated_at=observed_at,
            )
            stmt = insert(NetsuitePosting).values(**payload)
            await db.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_netsuite_postings_dedupe",
                    set_={k: getattr(stmt.excluded, k) for k in payload if k not in {"id", "tenant_id", "dedupe_key"}},
                )
            )
            count += 1
        await db.commit()  # Each payment's invalidation and replacement is atomic.
    return count
