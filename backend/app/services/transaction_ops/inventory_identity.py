"""Exact source-line binding through the inventory IDs stored by Framework's import.

No positional, subset or SKU-only match. Unknown/duplicate ownership invalidates
all bindings; the caller retains incomplete native evidence for investigation.
"""

import re

_ID = re.compile(r"[1-9][0-9]{0,29}\Z")


def sku(value):
    return (
        value
        if isinstance(value, str)
        and 0 < len(value) <= 255
        and value == value.strip()
        and all(ord(c) >= 32 for c in value)
        else None
    )


def _ids(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 500:
        return ()
    if any(type(value) not in (str, int) or not _ID.fullmatch(str(value)) for value in values):
        return ()
    result = tuple(sorted(str(value) for value in values))
    return result if len(set(result)) == len(result) else ()


def source_inventory(line):
    values = line.get("inventory_units")
    if not isinstance(values, list) or any(not isinstance(value, dict) for value in values):
        return ()
    return _ids([value.get("id") for value in values])


def native_inventory(value):
    if not isinstance(value, str) or len(value) > 15500:
        return ()
    return _ids([part.strip() for part in value.split(",")])


def unique_ownership(lines):
    seen = set()
    for line in lines:
        ids = line.inventory_unit_ids
        if not ids or line.sku is None or seen.intersection(ids):
            return False
        seen.update(ids)
    return bool(lines)


def native_bindings(raw_lines, source):
    if (
        source is None
        or not source.lines_complete
        or not unique_ownership(source.lines)
        or len(raw_lines) != len(source.lines)
    ):
        return {}
    by_inventory = {line.inventory_unit_ids: line for line in source.lines}
    result, seen, used_source = {}, set(), set()
    for raw in raw_lines:
        ids = native_inventory(raw.get("custcol_fw_inventory_unit_ids"))
        match = by_inventory.get(ids)
        line_id = str(raw.get("line"))
        if (
            not ids
            or seen.intersection(ids)
            or match is None
            or match.key in used_source
            or sku(raw.get("custcol_fw_original_ecom_sku")) != match.sku
            or line_id in result
        ):
            return {}
        result[line_id] = match
        used_source.add(match.key)
        seen.update(ids)
    return result
