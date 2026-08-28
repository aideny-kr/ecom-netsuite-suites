"""Build a link to a NetSuite record we just created or updated.

After a successful write the agent reports "Done" and the operator is left to
find the record themselves — search by name, or paste an internal id into a URL
whose shape they have to know. The create response carries the id, so the link
is constructible; it simply was not being built.

CONSERVATIVE BY DESIGN. An unknown record type, a missing account id, or a
missing record id yields NO link rather than a guessed one. A wrong link on a
financial record is worse than no link: it either lands on the wrong form or a
404, and both make an operator doubt a write that actually succeeded. Adding a
record type is one reviewed line in ``_PATHS``.
"""

from __future__ import annotations

from urllib.parse import quote

__all__ = ["build_record_url"]

# NetSuite record type -> the UI path that opens it.
#
# Every transaction shares `transaction.nl`, which resolves an id to the right
# form on its own. That is deliberate: one entry that NetSuite itself routes
# beats five per-type guesses that could each land on the wrong form.
_ENTITY_BASE = "/app/common/entity"
_TXN_PATH = "/app/accounting/transactions/transaction.nl"

_PATHS: dict[str, str] = {
    "customer": f"{_ENTITY_BASE}/custjob.nl",
    "vendor": f"{_ENTITY_BASE}/vendor.nl",
    "contact": f"{_ENTITY_BASE}/contact.nl",
    "employee": f"{_ENTITY_BASE}/employee.nl",
    "partner": f"{_ENTITY_BASE}/partner.nl",
    "invoice": _TXN_PATH,
    "salesorder": _TXN_PATH,
    "journalentry": _TXN_PATH,
    "vendorbill": _TXN_PATH,
    "creditmemo": _TXN_PATH,
    "customerpayment": _TXN_PATH,
    "customerdeposit": _TXN_PATH,
    "purchaseorder": _TXN_PATH,
}


def _host(account_id: str) -> str:
    """NetSuite's per-account host.

    The account id is lowercased and underscores become hyphens: a sandbox
    ``6738075_SB1`` is served from ``6738075-sb1.app.netsuite.com``. Getting
    this wrong produces a DNS failure, which reads to an operator as "the
    record isn't there".
    """
    return f"{account_id.strip().lower().replace('_', '-')}.app.netsuite.com"


def build_record_url(account_id: str | None, record_type: str | None, record_id: str | int | None) -> str | None:
    """Return a link to the record, or ``None`` when we cannot build one safely."""
    if not account_id or not record_type or record_id in (None, ""):
        return None

    path = _PATHS.get(str(record_type).strip().lower())
    if not path:
        return None

    return f"https://{_host(str(account_id))}{path}?id={quote(str(record_id).strip(), safe='')}"
