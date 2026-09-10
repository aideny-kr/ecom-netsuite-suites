"""Build and render links only for documents read in a verified NetSuite scope."""

import re
from urllib.parse import parse_qs, urlsplit

from app.services.transaction_ops.netsuite_reader import _account, _id

_PATHS = {
    "salesorder": "salesord.nl",
    "invoice": "custinvc.nl",
    "creditmemo": "custcred.nl",
    "depositapplication": "depositappl.nl",
    "customerdeposit": "custdep.nl",
    "cashsale": "cashsale.nl",
}
_ALIASES = {"credmemo.nl": "custcred.nl"}
_LINK = re.compile(r"\[([^\]]+)\]\((https://[^\s)]+)\)")


def evidence_record_links(evidence):
    scope = evidence.get("verified_connection_scope") or {}
    try:
        account = _account(scope.get("account_id"))
    except ValueError:
        return []
    sections = evidence.get("sections", {})
    records = [sections.get("sales_order", {})] + sections.get("posting_documents", []) + sections.get("deposits", [])
    records += list((sections.get("invoice_applications") or {}).get("documents", {}).values())
    links = []
    for doc in records:
        kind, ident = doc.get("record_type"), _id(doc.get("id"))
        if kind in _PATHS and ident:
            links.append(
                {
                    "record_type": kind,
                    "record_id": ident,
                    "label": doc.get("tranId", ident),
                    "url": f"https://{account}.app.netsuite.com/app/accounting/transactions/{_PATHS[kind]}?id={ident}",
                }
            )
    return links


def correct_record_links(text, tool_calls):
    links = [link for call in tool_calls or [] for link in call.get("record_links", [])]
    if not links:
        return text
    destinations = {}
    for link in links:
        url = urlsplit(link["url"])
        key = (url.path.rsplit("/", 1)[-1], link["record_id"])
        destinations.setdefault(key, set()).add(link["url"])

    def replace(match):
        url = urlsplit(match[2])
        if not (url.hostname or "").endswith(".app.netsuite.com") or "/transactions/" not in url.path:
            return match[0]
        path = url.path.rsplit("/", 1)[-1]
        ids = parse_qs(url.query).get("id", [])
        choices = destinations.get((_ALIASES.get(path, path), ids[0] if len(ids) == 1 else None), set())
        # Unverified/multi-account identities remain plain text, never a link
        # into an account selected from the model's default connector.
        return f"[{match[1]}]({next(iter(choices))})" if len(choices) == 1 else match[1]

    return _LINK.sub(replace, text)
