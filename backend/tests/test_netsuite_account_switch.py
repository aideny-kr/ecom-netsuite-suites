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

import html as html_mod
import uuid

from app.api.v1.netsuite_auth import (
    ACCOUNT_SWITCHED_HTML,
    CALLBACK_HTML,
    _js_string,
    _select_connection_for_account,
    _supersede_other_connections,
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
    # Both rows are active here -- the pathological duplicate state. PROD is among
    # them, so it is already serving some reads and this is not a switch. Naming
    # sandbox as "previous" would be arbitrary: which row served any given read was
    # precisely the nondeterminism being repaired.
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


def _render(previous: str = PROD, new: str = SANDBOX) -> str:
    """Render the switch page the way the callback does -- escaped for both contexts."""
    return ACCOUNT_SWITCHED_HTML.format(
        previous_account_id=html_mod.escape(previous),
        new_account_id=html_mod.escape(new),
        previous_account_id_js=_js_string(previous),
        new_account_id_js=_js_string(new),
    )


def test_switch_page_does_not_self_close():
    """The generic callback page self-closes after ~1s. That is not a way to tell
    somebody their reporting just moved to another NetSuite account."""
    assert "window.close" in CALLBACK_HTML, "baseline: the generic page does self-close"

    rendered = _render()

    assert "window.close" not in rendered
    assert "setTimeout" not in rendered


def test_switch_page_names_both_accounts_and_keeps_the_opener_contract():
    rendered = _render()

    assert PROD in rendered and SANDBOX in rendered
    # The frontend keys off event.data.type only (settings/page.tsx,
    # step-connection.tsx), so the switch page must still post the success type
    # or the connection UI never refreshes.
    assert "NETSUITE_AUTH_SUCCESS" in rendered
    assert "accountSwitchedFrom" in rendered


def test_switch_page_escapes_the_account_id_in_both_contexts():
    """account_id is user-supplied: it arrives as a query param on /authorize and
    rides through Redis unchanged, so it reaches this template untrusted."""
    hostile = '"><script>alert(1)</script>'

    rendered = _render(previous=hostile)

    assert "<script>alert(1)</script>" not in rendered, "raw payload reached the HTML body"
    assert "&lt;script&gt;" in rendered, "payload should be HTML-escaped, not stripped"
    # In the JS context json.dumps must have escaped the quote and the closing tag
    # cannot terminate the <script> element early.
    assert '"><script>' not in rendered


# ---------------------------------------------------------------------------
# The singleton the module assumes must be ENFORCED, not trusted
# ---------------------------------------------------------------------------


def test_unselected_rows_are_superseded_so_only_one_stays_active():
    """Two active rows make every downstream `.first()` nondeterministic.

    Consumers resolve the connection with no order_by (netsuite_suiteql.py:367,
    netsuite_connectivity.py:31, suitescript_sync_tool.py:28), so a stray second
    active row means SuiteQL can silently read the wrong NetSuite account.
    """
    keeper = _conn(PROD)
    stray = _conn(SANDBOX)

    superseded = _supersede_other_connections([keeper, stray], keeper, PROD)

    assert superseded == [stray]
    assert stray.status == "superseded"
    assert PROD in (stray.error_reason or "")
    assert keeper.status == "active", "the selected row must be left alone"


def test_supersede_is_not_revoke_so_the_row_can_be_reclaimed():
    """Revoking would strand the row: the callback only considers non-revoked
    candidates, so a later re-auth of that account would create a duplicate."""
    keeper, stray = _conn(PROD), _conn(SANDBOX)

    _supersede_other_connections([keeper, stray], keeper, PROD)

    assert stray.status != "revoked"
    reselected, _ = _select_connection_for_account([stray], SANDBOX)
    assert reselected is stray, "a superseded row must still be selectable on re-auth"


def test_already_revoked_rows_are_left_alone():
    revoked = _conn(SANDBOX)
    revoked.status = "revoked"
    keeper = _conn(PROD)

    assert _supersede_other_connections([keeper, revoked], keeper, PROD) == []
    assert revoked.status == "revoked"


def test_nothing_to_supersede_on_a_first_connect():
    assert _supersede_other_connections([], None, PROD) == []


# ---------------------------------------------------------------------------
# Superseded rows stay candidates -- so "switched" must be judged against the
# row actually serving reads, not against whichever row happens to match or sort first
# ---------------------------------------------------------------------------


def test_reconnecting_a_superseded_account_is_still_a_switch():
    """Gate round 3, major. Matching ANY candidate made this look like a no-op.

    Tenant is live on prod; sandbox sits superseded from an earlier switch. Re-auth
    sandbox: the old logic matched the sandbox row and reported switched_from=None,
    so no audit event, no warning page, no label rename -- while supersede flipped
    prod off underneath and every SuiteQL read silently moved to sandbox.
    """
    live_prod = _conn(PROD)
    old_sandbox = _conn(SANDBOX)
    old_sandbox.status = "superseded"

    selected, switched_from = _select_connection_for_account([live_prod, old_sandbox], SANDBOX)

    assert selected is old_sandbox, "reclaim the existing row rather than duplicating it"
    assert switched_from == PROD, "reads are moving off prod — that must be audited and surfaced"


def test_switched_from_names_the_active_account_not_a_stale_duplicate():
    """Gate round 3, major. candidates[0] is newest-touched, not necessarily active.

    A superseded duplicate touched more recently than the live row would have been
    reported as the previous account, putting a false account id in the audit trail
    and in the operator-facing warning.
    """
    stale_recent = _conn("9999999")
    stale_recent.status = "superseded"
    live_prod = _conn(PROD)

    selected, switched_from = _select_connection_for_account([stale_recent, live_prod], "newacct")

    assert switched_from == PROD, "must name the account whose reads were taken away"
    assert selected is live_prod, "must land on the row that was actually serving reads"


def test_reauth_of_the_active_account_is_still_not_a_switch():
    live_prod = _conn(PROD)
    old_sandbox = _conn(SANDBOX)
    old_sandbox.status = "superseded"

    selected, switched_from = _select_connection_for_account([live_prod, old_sandbox], PROD)

    assert selected is live_prod
    assert switched_from is None


def test_no_active_row_at_all_is_adopted_not_reported_as_a_switch():
    """Every row superseded (e.g. mid-recovery): there is no account losing reads."""
    orphan = _conn(SANDBOX)
    orphan.status = "superseded"

    selected, switched_from = _select_connection_for_account([orphan], PROD)

    assert selected is orphan
    assert switched_from is None
