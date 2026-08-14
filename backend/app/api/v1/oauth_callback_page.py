"""The popup page every NetSuite OAuth callback renders, with its escaping.

ONE copy on purpose. There used to be two near-verbatim templates -- `CALLBACK_HTML`
in netsuite_auth.py and `_MCP_CALLBACK_HTML` in mcp_connectors.py, differing only in a
<title> and two unused CSS rules -- and when the dual-context escaping was written it
was wired into one of them. Gate round 8 found the other still interpolating raw, and
it is reachable from the SAME unauthenticated endpoint: `/connections/netsuite/callback`
delegates to `netsuite_mcp_callback` for the MCP branch, so hardening one copy left the
route half-fixed. `netsuite_mcp_callback` has no route decorator of its own; that
delegation is its only caller, which is exactly why the duplicate went unnoticed.

Both interpolation contexts here are untrusted:
  * `<p>{message}</p>` -- carries the `error` query param and `str(exc)[:200]`
  * `error: {error_detail_js}` -- the same values, inside a JS string literal

`/callback` has no auth dependency and its error branch runs before any state or Redis
validation, so a payload needs no session and no valid state. There is no CSP on the
API origin, and the refresh cookie is host-only there with `path=/api/v1/auth`, so
injected script can POST /api/v1/auth/refresh same-origin and read an access token out
of the JSON body -- HttpOnly is no defence against same-origin script. That is why this
is a takeover primitive rather than a cosmetic reflection.
"""

from __future__ import annotations

import html
import json

CALLBACK_HTML = """<!DOCTYPE html>
<html>
<head>
  <title>{title}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; padding: 2rem; text-align: center; }}
    .success {{ color: green; }}
    .error {{ color: red; }}
    a {{ color: #0066cc; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
  </style>
</head>
<body>
  <h3 class="{status}">
    {heading}
  </h3>
  <p>{message}</p>
  <script>
    try {{
      if (window.opener) {{
        window.opener.postMessage(
          {{ type: "{event_type}", error: {error_detail_js} }},
          "*"
        );
        setTimeout(function() {{ window.close(); }}, 1000);
      }} else {{
        setTimeout(function() {{ window.location.href = "/"; }}, 2000);
      }}
    }} catch (e) {{
      window.location.href = "/";
    }}
  </script>
</body>
</html>"""


def js_string(value: str) -> str:
    """JSON-encode a value for embedding inside an inline <script> block.

    json.dumps alone is NOT enough here: it escapes quotes but leaves `<` and `>`
    untouched, so a value containing `</script>` closes the element early and
    everything after it is parsed as HTML. Escaping the angle brackets (and `&`)
    to \\u00xx keeps the string inert wherever it lands.

    It supplies its own surrounding quotes, so the template must not add any.
    """
    return json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render_callback(
    *,
    status: str,
    heading: str,
    message: str,
    event_type: str,
    error_detail: str,
    title: str = "NetSuite Authentication",
) -> str:
    """Render the callback page with BOTH interpolation contexts escaped.

    A single funnel, so that no call site can forget: there are ten of them across two
    modules, and several pass `str(exc)[:200]` or a raw query param straight through.
    """
    return CALLBACK_HTML.format(
        title=html.escape(title),
        status=html.escape(status),
        heading=html.escape(heading),
        message=html.escape(message),
        event_type=html.escape(event_type),
        error_detail_js=js_string(error_detail),
    )
