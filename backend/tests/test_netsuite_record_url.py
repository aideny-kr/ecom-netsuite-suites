"""A link to the record we just created.

After a successful write the agent says "Done" and the operator has to go find
the record themselves — search NetSuite by name, or paste an internal id into a
URL they have to know the shape of. The id comes back in the create response,
so the link is constructible and simply was not being built.

Deliberately conservative: an unknown record type or a missing account id
yields NO link rather than a guessed one. A wrong link on a financial record is
worse than no link — it sends someone to confirm the wrong thing, or to a 404
that makes them doubt the write landed at all.
"""

from __future__ import annotations

import pytest

from app.services.chat.netsuite_record_url import build_record_url


def test_customer_link():
    assert build_record_url("6738075", "customer", "5795008") == (
        "https://6738075.app.netsuite.com/app/common/entity/custjob.nl?id=5795008"
    )


def test_vendor_uses_its_own_path():
    assert build_record_url("6738075", "vendor", "42") == (
        "https://6738075.app.netsuite.com/app/common/entity/vendor.nl?id=42"
    )


@pytest.mark.parametrize("record_type", ["invoice", "salesOrder", "journalEntry", "vendorBill", "creditMemo"])
def test_transactions_share_the_generic_transaction_path(record_type):
    """NetSuite's transaction.nl resolves any transaction id to the right form
    on its own, so one entry covers every transaction type — and cannot send
    someone to the WRONG form the way a per-type guess could."""
    url = build_record_url("6738075", record_type, "99")
    assert url == "https://6738075.app.netsuite.com/app/accounting/transactions/transaction.nl?id=99"


def test_record_type_matching_is_case_insensitive():
    """record_type reaches us from model-composed tool input; NetSuite spells
    it camelCase over REST and lowercase in SuiteQL."""
    assert build_record_url("6738075", "SalesOrder", "1") == build_record_url("6738075", "salesorder", "1")
    assert build_record_url("6738075", "CUSTOMER", "1") == build_record_url("6738075", "customer", "1")


def test_sandbox_account_id_becomes_a_valid_host():
    """NetSuite sandbox account ids carry an underscore (6738075_SB1) but the
    host uses a hyphen and lowercase. Getting this wrong yields a DNS failure,
    which looks to an operator like the record does not exist."""
    assert build_record_url("6738075_SB1", "customer", "7") == (
        "https://6738075-sb1.app.netsuite.com/app/common/entity/custjob.nl?id=7"
    )


def test_unknown_record_type_yields_no_link():
    """No link beats a guessed one: a wrong path lands on the wrong form or a
    404, and either makes an operator doubt a write that actually succeeded."""
    assert build_record_url("6738075", "someCustomRecord", "1") is None


@pytest.mark.parametrize(
    "account,rtype,rid",
    [
        (None, "customer", "1"),
        ("", "customer", "1"),
        ("6738075", "customer", None),
        ("6738075", "customer", ""),
        ("6738075", None, "1"),
    ],
)
def test_missing_inputs_yield_no_link(account, rtype, rid):
    assert build_record_url(account, rtype, rid) is None


def test_record_id_is_url_safe():
    """The id goes into a query string. It is server-supplied today, but the
    function must not be the weak point if that ever changes."""
    url = build_record_url("6738075", "customer", "5 795008")
    assert url is not None and " " not in url
