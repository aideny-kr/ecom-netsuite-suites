"""NetSuite REST OAuth callback: which connection row does a callback land on?

The tenant-wide NetSuite REST connection is a SINGLETON by design -- every consumer
resolves it as `provider=="netsuite" AND status=="active"` -> `.first()`/`LIMIT 1`
(netsuite_suiteql.py, pivot_tool.py, cross_source_tool.py, netsuite_connectivity.py,
suitescript_sync_tool.py, suitescript_sync.py). So the callback updates one row in
place rather than adding a row per account.

The bug this file pins: the pre-fix upsert filtered only on tenant+provider+status
and took `ORDER BY updated_at DESC LIMIT 1`, with no account_id filter. Authorizing
a sandbox therefore overwrote the prod row's credentials while `label` -- only ever
set on the create path -- kept naming prod. Every SuiteQL query, pivot, report and
sync silently followed the sandbox with nothing in the UI to say so.
"""

from __future__ import annotations

import uuid

from app.api.v1.netsuite_auth import (
    ACCOUNT_SWITCHED_HTML,
    CALLBACK_HTML,
    _select_connection_for_account,
)
from app.models.connection import Connection

PROD = "1234567"
SANDBOX = "1234567-sb1"


def _conn(account_id: str | None, *, label: str | None = None, legacy: bool = False) -> Connection:
    """A detached Connection row. No session needed -- the helper is pure."""
    if legacy:
        metadata_json = None
    elif account_id is None:
        metadata_json = {"auth_type": "oauth2"}
    else:
        metadata_json = {"account_id": account_id, "auth_type": "oauth2"}
    return Connection(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        provider="netsuite",
        label=label or f"NetSuite {account_id or 'unknown'}",
        status="active",
        auth_type="oauth2",
        encrypted_credentials="x",
        metadata_json=metadata_json,
    )


def test_no_existing_rows_creates_new():
    connection, switched_from = _select_connection_for_account([], PROD)
    assert connection is None
    assert switched_from is None


def test_reauth_same_account_updates_in_place():
    existing = _conn(PROD)
    connection, switched_from = _select_connection_for_account([existing], PROD)
    assert connection is existing
    assert switched_from is None, "re-authorizing the same account is not a switch"


def test_prefers_row_for_this_account_over_more_recent_other_account():
    """The core defect: candidates arrive newest-first, and the old code took [0].

    A tenant with prod connected who re-authorizes prod must land on the PROD row
    even when a sandbox row sorts ahead of it.
    """
    sandbox_row = _conn(SANDBOX)  # most recently updated -> first in the list
    prod_row = _conn(PROD)
    connection, switched_from = _select_connection_for_account([sandbox_row, prod_row], PROD)
    assert connection is prod_row, "must not overwrite the sandbox row with prod credentials"
    assert switched_from is None


def test_switching_accounts_reports_the_previous_account():
    """Operator decision 2026-08-06: overwrite, but loudly.

    The switch still happens (singleton invariant), but the caller gets the previous
    account id so it can refresh the label, audit the change and tell the user.
    """
    existing = _conn(PROD)
    connection, switched_from = _select_connection_for_account([existing], SANDBOX)
    assert connection is existing
    assert switched_from == PROD


def test_legacy_row_without_metadata_is_adopted_not_reported_as_switch():
    """Rows written before metadata_json carried account_id must not look like a switch.

    We cannot know which account they belong to, so claiming "switched from X" would
    be a fabricated audit record. Adopt them silently; the label refresh still fixes
    the naming.
    """
    legacy = _conn(None, label="NetSuite", legacy=True)
    connection, switched_from = _select_connection_for_account([legacy], PROD)
    assert connection is legacy
    assert switched_from is None


def test_legacy_row_with_metadata_but_no_account_id_is_adopted():
    legacy = _conn(None)
    connection, switched_from = _select_connection_for_account([legacy], PROD)
    assert connection is legacy
    assert switched_from is None


def test_switch_is_not_reported_when_previous_account_matches():
    """Guards against reporting a switch on a no-op re-auth of a single row."""
    existing = _conn(PROD)
    _, switched_from = _select_connection_for_account([existing], PROD)
    assert switched_from is None


# ---------------------------------------------------------------------------
# The "loudly" half of the decision
# ---------------------------------------------------------------------------


def test_switch_page_does_not_self_close():
    """The generic callback page self-closes after ~1s. That is not a way to tell
    somebody their reporting just moved to another NetSuite account."""
    assert "window.close" in CALLBACK_HTML, "baseline: the generic page does self-close"

    html = ACCOUNT_SWITCHED_HTML.format(previous_account_id=PROD, new_account_id=SANDBOX)

    assert "window.close" not in html
    assert "setTimeout" not in html


def test_switch_page_names_both_accounts_and_keeps_the_opener_contract():
    html = ACCOUNT_SWITCHED_HTML.format(previous_account_id=PROD, new_account_id=SANDBOX)

    assert PROD in html and SANDBOX in html
    # The frontend keys off event.data.type only (settings/page.tsx,
    # step-connection.tsx), so the switch page must still post the success type
    # or the connection UI never refreshes.
    assert "NETSUITE_AUTH_SUCCESS" in html
    assert "accountSwitchedFrom" in html
