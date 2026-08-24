# NetSuite `ns_getRecordTypeMetadata` fixture — not yet captured

`record_metadata_service.py::_parse_field` decides whether a NetSuite field is
required. As of 2026-08-23 that logic has **never been checked against a live
`ns_getRecordTypeMetadata` response** — the NetSuite MCP token is expired, and
every existing test in `test_record_metadata_service.py` uses our own
invented fixture shape (`{"mandatory": true/false}`).

We know from other handlers in this repo that NetSuite serialises booleans
inconsistently across endpoints:
- `prompt_template_service.py:87` reads discovered account metadata and
  checks `f.get("ismandatory") == "T"` (string, not bool).
- `posting_invariants.py:157` handles the same `"T"`/`"TRUE"`/`"YES"`
  convention for a different closed/open flag.

Because of that, `_parse_field` is **tolerant-by-necessity, not verified**:
it accepts `mandatory` / `ismandatory` / `required` / `isrequired` /
`isMandatory` / `isRequired` as the marker key (first one present wins), and
coerces the value as truthy for real `True` and the strings `"T"`, `"TRUE"`,
`"YES"`, `"1"` (case-insensitive), falsy otherwise. If a response contains
fields but none of them carry any of those keys, it logs a `logger.warning`
naming the record type and the keys actually observed — loud, not fatal.

**All of the above (which key NetSuite actually uses, whether it's a bool or
a string, whether it's the same across record types) is currently guessed.**

## What to do the moment the MCP token is re-authorised

1. Call `ns_getRecordTypeMetadata` for a couple of real record types (e.g.
   `customer`, `journalentry`) via the reconnected MCP tool.
2. Save the raw JSON response(s) here, e.g.
   `customer_metadata.json`, `journalentry_metadata.json` — no
   modification, this should be the literal wire response.
3. Add a test in `test_record_metadata_service.py` that loads the captured
   fixture and asserts `get_record_metadata()` parses the *actual* required
   fields correctly (cross-check a few field names by hand against the NetSuite
   UI or documentation).
4. If the real shape uses a key or boolean encoding NOT already handled by
   `_REQUIRED_MARKER_KEYS` / `_coerce_required_flag` in
   `record_metadata_service.py`, extend those — don't special-case the
   fixture.
5. Once a real fixture is in place, the tolerant-multi-key guessing can be
   narrowed if desired, but keep the other accepted spellings as a safety net
   unless you've confirmed NetSuite is consistent across every record type.
