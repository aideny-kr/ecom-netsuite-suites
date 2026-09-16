"""Preserve a single invoice's verified accounting classifications on its credit."""

from app.services.transaction_ops.netsuite_reader import _collection, _id

FIELDS = ("location", "department", "class")
RECORD_TYPES = {"location": "location", "department": "department", "class": "classification"}


def reference(record, field):
    value = record.get(field)
    if value is None:
        return None
    if not isinstance(value, dict) or not _id(value.get("id")):
        raise ValueError(f"credit_{field}_invalid")
    return _id(value["id"])


def verified_fields(invoice, support, subsidiary):
    """Never copy another order's location or collapse conflicting line dimensions.

    This narrow recipe requires an explicit invoice Location even on accounts
    where that field may be optional. Other classifications are preserved when
    present. An ambiguous allocation needs its own reviewed treatment.
    """
    if not reference(invoice, "location"):
        raise ValueError("Credit requires a verified invoice Location; refresh the accounting evidence.")
    lines = support.get("classification_lines")
    if support.get("classification_lines_complete") is not True or not isinstance(lines, list) or not lines:
        raise ValueError("Complete invoice line classifications are required before preparing the credit.")
    result = {}
    for field in FIELDS:
        identifier = reference(invoice, field)
        if any(reference(line, field) not in (None, identifier) for line in lines):
            raise ValueError(
                f"Invoice has conflicting {field} allocations; a single credit allocation is not verified."
            )
        if identifier is None:
            continue
        record = support["classification_records"][field]
        rows, complete = _collection(record.get("subsidiary") or {})
        if (
            str(record.get("id")) != identifier
            or record.get("isInactive") is not False
            or not complete
            or str(subsidiary) not in {_id(row.get("id")) for row in rows}
        ):
            raise ValueError(f"Invoice {field} is not verified active for the credit subsidiary.")
        result[field] = {"id": identifier}
    return result


def matches_approved(record, proposal):
    """A successful amount check cannot hide a changed accounting classification."""
    try:
        return (
            record.get("lines_complete") is True
            and bool(record.get("line_items"))
            and all(
                reference(record, field) == reference(proposal["proposed_fields"], field)
                and all(reference(line, field) in (None, reference(record, field)) for line in record["line_items"])
                for field in FIELDS
            )
        )
    except (TypeError, ValueError):
        return False
