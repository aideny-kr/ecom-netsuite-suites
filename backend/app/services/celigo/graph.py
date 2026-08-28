"""Recursive script-reference walker for Celigo objects.

Plan B scope: find every script attachment site in a flow/export/import/etc,
so later tasks can map scripts to the steps that use them (Task 5+).

THE CORE DESIGN POINT (plan's Verified Facts, reaffirmed live per
observed-shapes.md): scripts attach at `transform.script`, `filter.script`,
`hooks.*` (an open-ended, never-enumerated key set), and router branches --
but that is NOT a fixed site list to enumerate. This module walks the WHOLE
object tree recursively and collects every `_scriptId` it finds, wherever it
occurs. A new hook type, or an attachment site nobody has documented, is
found automatically -- no code change needed here to recognize it. This
mirrors `sanitizer.py`'s `hooks.*` wildcard reasoning (never hardcode a
taxonomy for something Celigo can extend without telling us) but goes one
step further: even the CONTAINER names (`hooks`, `filter`, `transform`,
`routers`) are not load-bearing for detection, only `_scriptId`'s presence
is.

Two real negative cases (observed-shapes.md, live-confirmed 2026-08-27):
`filter`/`transform` of `type: "expression"` carry NO `_scriptId` and must
yield no ref. A walker keyed on the `filter`/`transform` key, or on `type`,
would manufacture a false ref here -- only `_scriptId`'s presence may decide.

Router caveat (observed-shapes.md, probed across 60 real flows): every
`routers[].script` observed live is `{function: "branching"}` with NO
`_scriptId` (all used `routeRecordsUsing: "input_filters"`, never
`"script"`). This walker still covers that site -- and any hypothetical
future one where a router branch DOES carry a `_scriptId` -- for free,
because it is a generic recursive walk, not an enumerated site list.

`site_type` is a BEST-EFFORT label for reporting, derived from the path a ref
was found at (does it pass through a `hooks`/`filter`/`transform`/`routers`
segment). It plays no part in DECIDING whether something is a script ref --
that decision is `_scriptId`'s presence alone -- so an unrecognized path
segment can never cause a real ref to be dropped; it only falls back to the
`"unknown"` label.
"""

from __future__ import annotations

from dataclasses import dataclass

# Path segment -> site_type label. Checked against each `.`-separated segment
# of a found ref's path (list-index suffixes like `[0]` stripped first). Order
# doesn't matter in practice -- these markers don't co-occur in one path in
# any observed shape -- but the loop takes the first match if they ever did.
_SITE_TYPE_BY_PATH_SEGMENT: dict[str, str] = {
    "hooks": "hook",
    "filter": "filter",
    "transform": "transform",
    "routers": "router",
}

_ROOT_PATH = "$"  # sentinel for a ref found with no path prefix at all


@dataclass(frozen=True)
class ScriptRef:
    """One `_scriptId` occurrence found somewhere in a walked object."""

    script_id: str
    function_name: str | None
    json_path: str
    site_type: str


def walk_script_refs(obj: dict) -> list[ScriptRef]:
    """Recursively find every `_scriptId` in *obj* and return one ScriptRef
    per occurrence, in traversal order.

    Does not mutate *obj*. Never enumerates Celigo's attachment-site
    taxonomy -- see module docstring.
    """
    return list(_walk(obj, ""))


def _walk(node: object, path: str):
    """Depth-first walk of dicts and lists. Yields a ScriptRef for every dict
    that carries a valid `_scriptId`, then keeps recursing into that SAME
    dict's other keys (a `_scriptId` container has never been observed to
    nest another one inside it, but nothing here assumes that -- the walk
    doesn't stop early just because it found one)."""
    if isinstance(node, dict):
        script_id = node.get("_scriptId")
        if isinstance(script_id, str) and script_id:
            function_name = node.get("function")
            yield ScriptRef(
                script_id=script_id,
                function_name=function_name if isinstance(function_name, str) else None,
                json_path=path or _ROOT_PATH,
                site_type=_classify_site_type(path),
            )
        for key, value in node.items():
            child_path = f"{path}.{key}" if path else key
            yield from _walk(value, child_path)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _walk(item, f"{path}[{index}]")
    # else: a leaf scalar (str/int/bool/None/...) -- nothing to recurse into.


def _classify_site_type(path: str) -> str:
    """Best-effort label from the path a ref was found at. See module
    docstring: this affects only the `site_type` field, never whether
    something IS a ref -- an unrecognized path just falls back to
    `"unknown"` rather than losing the ref."""
    for segment in path.split("."):
        name = segment.split("[", 1)[0]  # strip a trailing `[N]` list index
        site_type = _SITE_TYPE_BY_PATH_SEGMENT.get(name)
        if site_type is not None:
            return site_type
    return "unknown"
