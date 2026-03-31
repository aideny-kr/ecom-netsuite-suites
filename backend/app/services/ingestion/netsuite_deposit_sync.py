"""NetSuite deposit sync: pull bank deposits via SuiteQL and upsert into netsuite_postings.

Uses the tenant's existing OAuth REST connection — does NOT create a new connector.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import decrypt_credentials
from app.models.canonical import NetsuitePosting
from app.models.connection import Connection
from app.services.netsuite_client import execute_suiteql_via_rest
from app.services.netsuite_oauth_service import get_valid_token

logger = structlog.get_logger()

# Regex patterns for extracting Stripe payout IDs from memo/description fields
_PAYOUT_ID_PATTERNS = [
    re.compile(r"(po_[A-Za-z0-9]{20,30})"),  # Standard Stripe payout ID
    re.compile(r"payout[:\s_-]*([A-Za-z0-9]{20,30})", re.IGNORECASE),
    re.compile(r"stripe\.com/payouts/(\w+)", re.IGNORECASE),
]

# Regex for extracting order references (R followed by 9 digits) from sales order display names
_ORDER_REF_PATTERN = re.compile(r"(R\d{9})")

# SuiteQL query template — pagination handled by execute_suiteql_via_rest(paginate=True)
_DEPOSIT_QUERY = """\
SELECT
    t.id AS internal_id,
    t.tranid AS document_number,
    t.trandate AS transaction_date,
    t.type AS record_type,
    t.memo,
    t.total AS amount,
    BUILTIN.DF(t.currency) AS currency_name,
    t.subsidiary AS subsidiary_id,
    BUILTIN.DF(t.subsidiary) AS subsidiary_name,
    t.account AS account_id,
    BUILTIN.DF(t.account) AS account_name,
    t.status,
    BUILTIN.DF(tl.createdfrom) AS sales_order_ref
FROM transaction t
JOIN transactionline tl ON tl.transaction = t.id AND tl.mainline = 'T'
WHERE
    t.type IN ('Deposit', 'CustDep')
    AND t.trandate >= TO_DATE('{date_from}', 'YYYY-MM-DD')
    AND t.trandate <= TO_DATE('{date_to}', 'YYYY-MM-DD')
    AND t.posting = 'T'
ORDER BY t.trandate DESC\
"""


@dataclass
class DepositSyncResult:
    """Summary of a deposit sync run."""

    records_synced: int = 0
    records_updated: int = 0
    records_new: int = 0
    errors: list[str] = field(default_factory=list)


def extract_order_ref(sales_order_ref: str | None) -> str | None:
    """Extract an order reference (R followed by 9 digits) from a sales order display name.

    E.g. "Sales Order #R577684612" → "R577684612"
    Returns the first match, or None.
    """
    if not sales_order_ref:
        return None
    m = _ORDER_REF_PATTERN.search(sales_order_ref)
    return m.group(1) if m else None


def extract_payout_id(memo: str | None) -> str | None:
    """Extract a Stripe payout ID from a memo string.

    Returns the first match, or None.
    """
    if not memo:
        return None
    for pattern in _PAYOUT_ID_PATTERNS:
        m = pattern.search(memo)
        if m:
            payout_id = m.group(1) if pattern.groups else m.group(0)
            # Normalize: ensure po_ prefix for standard IDs
            if payout_id.startswith("po_"):
                return payout_id
            return f"po_{payout_id}" if not payout_id.startswith("po_") else payout_id
    return None


async def get_netsuite_rest_connection(db: AsyncSession, tenant_id: str) -> Connection | None:
    """Find the active NetSuite REST OAuth connection for a tenant."""
    result = await db.execute(
        select(Connection).where(
            Connection.tenant_id == tenant_id,
            Connection.provider == "netsuite",
            Connection.status.in_(["active", "healthy"]),
        )
    )
    return result.scalar_one_or_none()


async def sync_netsuite_deposits(
    db: AsyncSession,
    tenant_id: str,
    date_from: date,
    date_to: date,
    account_filter: list[str] | None = None,
    record_types: list[str] | None = None,
) -> DepositSyncResult:
    """Pull bank deposits from NetSuite via SuiteQL and upsert into netsuite_postings.

    Args:
        db: Async database session
        tenant_id: Tenant UUID
        date_from: Start date for deposit query
        date_to: End date for deposit query
        account_filter: Optional list of GL account IDs to restrict to
        record_types: Optional list of record types (default: ['Deposit', 'CustDep'])
    """
    result = DepositSyncResult()

    # 1. Get active NetSuite REST connection
    connection = await get_netsuite_rest_connection(db, tenant_id)
    if not connection:
        result.errors.append("No active NetSuite REST connection found")
        return result

    # 2. Get valid OAuth token
    token = await get_valid_token(db, connection)
    if not token:
        result.errors.append("Failed to get valid NetSuite OAuth token")
        return result

    # 3. Build and execute SuiteQL query
    creds = decrypt_credentials(connection.encrypted_credentials)
    account_id = creds.get("account_id", "")

    # Build type filter
    types = record_types or ["Deposit", "CustDep"]
    type_clause = ", ".join(f"'{t}'" for t in types)

    query = _DEPOSIT_QUERY.replace("t.type IN ('Deposit', 'CustDep')", f"t.type IN ({type_clause})").format(
        date_from=date_from.isoformat(),
        date_to=date_to.isoformat(),
    )

    try:
        data = await execute_suiteql_via_rest(
            access_token=token,
            account_id=account_id,
            query=query,
            limit=50000,
            paginate=True,
        )
    except Exception as e:
        result.errors.append(f"SuiteQL query failed: {e}")
        logger.error("netsuite_deposit_sync.query_failed", error=str(e), tenant_id=tenant_id)
        return result

    columns = data.get("columns", [])
    rows = data.get("rows", [])

    logger.info(
        "netsuite_deposit_sync.query_complete",
        tenant_id=tenant_id,
        rows_returned=len(rows),
    )

    # 4. Upsert each deposit into netsuite_postings
    for row in rows:
        row_dict = dict(zip(columns, row)) if isinstance(row, list) else row
        internal_id = str(row_dict.get("internal_id", ""))
        if not internal_id:
            continue

        memo = row_dict.get("memo") or ""
        record_type = str(row_dict.get("record_type", "")).lower()

        # Apply account filter if specified
        row_account_id = str(row_dict.get("account_id", ""))
        if account_filter and row_account_id not in account_filter:
            continue

        # Extract order reference from linked sales order (primary)
        sales_order_ref = row_dict.get("sales_order_ref", "")
        order_ref = extract_order_ref(sales_order_ref) if sales_order_ref else None

        # Fallback: extract payout ID from memo (legacy path)
        payout_id = extract_payout_id(memo) if not order_ref else None

        # Parse amount
        try:
            amount = Decimal(str(row_dict.get("amount", 0)))
        except Exception:
            amount = Decimal("0")

        # Parse currency — use name if available, fall back to ID
        currency_name = row_dict.get("currency_name", "")
        currency = _normalize_currency(currency_name) if currency_name else "USD"

        # Parse transaction date
        txn_date_raw = row_dict.get("transaction_date")
        txn_date = _parse_date(txn_date_raw) if txn_date_raw else None

        dedupe_key = f"netsuite:{internal_id}"

        # Check if record already exists
        existing = await db.execute(
            select(NetsuitePosting.id).where(
                NetsuitePosting.tenant_id == tenant_id,
                NetsuitePosting.dedupe_key == dedupe_key,
            )
        )
        is_new = existing.scalar_one_or_none() is None

        # Use raw SQL upsert for performance
        from datetime import datetime, timezone

        from sqlalchemy.dialects.postgresql import insert

        now = datetime.now(timezone.utc)
        values = {
            "id": uuid.uuid4(),
            "tenant_id": tenant_id,
            "dedupe_key": dedupe_key,
            "source": "netsuite",
            "source_id": internal_id,
            "netsuite_internal_id": internal_id,
            "record_type": record_type,
            "transaction_date": txn_date,
            "amount": amount,
            "currency": currency,
            "account_id": row_account_id,
            "account_name": row_dict.get("account_name"),
            "subsidiary_id": str(row_dict.get("subsidiary_id", "")) or None,
            "memo": memo or None,
            "related_payout_id": order_ref or payout_id,
            "raw_data": row_dict,
            "created_at": now,
            "updated_at": now,
        }

        excluded_keys = {"id", "tenant_id", "dedupe_key", "created_at"}
        set_ = {k: v for k, v in values.items() if k not in excluded_keys}
        set_["updated_at"] = now

        stmt = (
            insert(NetsuitePosting)
            .values(**values)
            .on_conflict_do_update(
                constraint="uq_netsuite_postings_dedupe",
                set_=set_,
            )
        )
        await db.execute(stmt)

        result.records_synced += 1
        if is_new:
            result.records_new += 1
        else:
            result.records_updated += 1

        # Batch commit every 10 records — Supabase has 2min statement timeout
        if result.records_synced % 10 == 0:
            await db.commit()

    await db.commit()

    logger.info(
        "netsuite_deposit_sync.complete",
        tenant_id=tenant_id,
        synced=result.records_synced,
        new=result.records_new,
        updated=result.records_updated,
    )
    return result


def _normalize_currency(currency_name: str) -> str:
    """Normalize NetSuite currency display name to 3-letter ISO code."""
    name = currency_name.strip().upper()
    # Common NetSuite currency display names
    mapping = {
        "USA": "USD",
        "US DOLLAR": "USD",
        "USD": "USD",
        "EURO": "EUR",
        "EUR": "EUR",
        "BRITISH POUND": "GBP",
        "GBP": "GBP",
        "CANADIAN DOLLAR": "CAD",
        "CAD": "CAD",
        "JAPANESE YEN": "JPY",
        "JPY": "JPY",
        "AUSTRALIAN DOLLAR": "AUD",
        "AUD": "AUD",
    }
    # If it's already a 3-letter code, use it
    if len(name) == 3 and name.isalpha():
        return name
    return mapping.get(name, "USD")


def _parse_date(value: str | None) -> date | None:
    """Parse a date string from SuiteQL response."""
    if not value:
        return None
    # SuiteQL returns dates as "M/D/YYYY" or "YYYY-MM-DD"
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            from datetime import datetime

            return datetime.strptime(str(value), fmt).date()
        except ValueError:
            continue
    return None
