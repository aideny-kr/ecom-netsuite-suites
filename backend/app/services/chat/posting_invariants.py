"""Two posting invariants that record-type metadata cannot express.

Deliberately only two: the accounting period must be open, and a journal entry
must balance. Amount provenance, approval envelopes and posting budgets belong
to the autonomous-accounting-ops program, not here.
"""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.chat.tools import execute_tool_call
from app.services.chat.write_payload import NormalizedPayload

logger = logging.getLogger(__name__)

# trandate goes into a raw SuiteQL string below (ns_runCustomSuiteQL takes a
# query, not bind params) — anchor to exactly YYYY-MM-DD before it is ever
# interpolated, so a crafted payload field cannot break out of the literal.
# \A/\Z (not ^/$) so a trailing newline can't slip past the anchor.
_DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")

# Record types that post to the general ledger. Only these get invariant checks.
_TRANSACTION_TYPES: frozenset[str] = frozenset(
    {
        "journalEntry",
        "invoice",
        "creditMemo",
        "customerPayment",
        "customerDeposit",
        "vendorBill",
        "vendorPayment",
        "vendorCredit",
        "expenseReport",
        "deposit",
        "check",
    }
)

_BALANCED_TYPES: frozenset[str] = frozenset({"journalEntry"})


class _AmountUnreadableError(ValueError):
    """A debit/credit value could not be read as a number."""


def _to_decimal(value: Any) -> Decimal:
    """Absent is zero; unreadable is an error.

    Coercing a garbage amount to Decimal(0) would let a journal entry whose
    debit AND credit are both unreadable compare 0 == 0 and report itself as
    BALANCED — a silent pass in the one check that exists to protect the
    ledger. Absent/empty is genuinely zero; anything else must surface.
    """
    if value in (None, ""):
        return Decimal(0)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise _AmountUnreadableError(str(value)) from exc


def _check_balanced(payload: NormalizedPayload) -> list[str]:
    debits = Decimal(0)
    credits = Decimal(0)
    for idx, line in enumerate(payload.lines):
        for key, bucket in (("debit", "debits"), ("credit", "credits")):
            try:
                amount = _to_decimal(line.get(key))
            except _AmountUnreadableError as exc:
                return [
                    f"Journal entry line {idx} has an unreadable {key} amount: {exc}. "
                    "The entry cannot be confirmed to balance."
                ]
            if bucket == "debits":
                debits += amount
            else:
                credits += amount

    if debits != credits:
        return [f"Journal entry does not balance: debits {debits} != credits {credits}."]
    return []


async def _check_period_open(payload: NormalizedPayload, **kw: Any) -> list[str]:
    tran_date = payload.fields.get("trandate") or payload.fields.get("tranDate")
    if not tran_date or not _DATE_RE.match(str(tran_date)):
        # No date, or not a clean YYYY-MM-DD — we cannot safely query on it
        # (see _DATE_RE) and cannot determine period state either way.
        return []

    from app.services.chat.tools import _make_ext_tool_name, parse_external_tool_name

    parsed = parse_external_tool_name(kw["mutation_tool_name"])
    if not parsed:
        return []

    query = (
        "SELECT periodname, closed FROM accountingperiod "
        f"WHERE startdate <= TO_DATE('{tran_date}', 'YYYY-MM-DD') "
        f"AND enddate >= TO_DATE('{tran_date}', 'YYYY-MM-DD') "
        "AND isquarter = 'F' AND isyear = 'F'"
    )
    # Same guarantee as record_metadata_service: the ENTIRE fetch-and-parse
    # body is guarded, so a malformed-but-valid-JSON response cannot raise out
    # of an invariant check. Note the asymmetry with metadata lookup — here an
    # indeterminate result returns [] (no invariant violation asserted), because
    # fabricating a "period is closed" error from a parse failure would block
    # legitimate writes. The tradeoff is deliberate: we never invent a
    # violation, and a period we could not read is reported by the card's
    # unvalidated marker rather than by a false error here.
    try:
        raw = await execute_tool_call(
            tool_name=_make_ext_tool_name(parsed[0], "ns_runCustomSuiteQL"),
            tool_input={"query": query},
            tenant_id=kw["tenant_id"],
            actor_id=kw["actor_id"],
            correlation_id=kw["correlation_id"],
            db=kw["db"],
            session_id=kw["session_id"],
        )
        data = json.loads(raw)

        if not isinstance(data, dict):
            return []
        rows = data.get("items") or data.get("data") or []
        if not isinstance(rows, list):
            return []

        for row in rows:
            if not isinstance(row, dict):
                continue
            closed = str(row.get("closed", "")).strip().upper()
            if closed in ("T", "TRUE", "YES"):
                name = row.get("periodname", tran_date)
                return [f"Accounting period '{name}' is closed — posting is not permitted."]
    except Exception:
        # Cannot determine period state — do not fabricate a pass or a fail.
        logger.warning("posting_invariants: period lookup failed", exc_info=True)
        return []

    return []


async def check_posting_invariants(
    *,
    payload: NormalizedPayload,
    record_type: str,
    mutation_tool_name: str,
    tenant_id: Any,
    actor_id: Any,
    correlation_id: str,
    db: Any,
    session_id: str,
) -> list[str]:
    """Return invariant violations, empty when clean."""
    if record_type not in _TRANSACTION_TYPES:
        return []

    errors: list[str] = []
    if record_type in _BALANCED_TYPES:
        errors.extend(_check_balanced(payload))

    errors.extend(
        await _check_period_open(
            payload,
            mutation_tool_name=mutation_tool_name,
            tenant_id=tenant_id,
            actor_id=actor_id,
            correlation_id=correlation_id,
            db=db,
            session_id=session_id,
        )
    )
    return errors
