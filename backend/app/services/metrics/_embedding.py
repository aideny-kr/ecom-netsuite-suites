"""The ONE source of the metric intent-embedding source string.

Shared by BOTH writer paths so the SAME metric is always embedded from a
byte-identical source string regardless of which path last wrote the row:

- the seeder (``metric_catalog_seeder.seed_system_metrics`` → ``embed_domain_texts``)
- the authoring path (``metric_authoring.create_metric`` / ``update_metric`` →
  ``embed_domain_query``)

Two non-identical copies of this helper previously existed (one per path). For a
payload with an empty ``definition`` they diverged (``'Cash |  | cash balance'``
vs ``'Cash | cash balance'``) → different 1536-d vectors for the "same" metric →
the metric resolver would rank it inconsistently purely by which path last wrote
the row. This is the single canonical helper that closes that hazard.

The filter-empties behavior is LOAD-BEARING: an empty ``definition`` (or any
empty part) must NOT inject a double-bar artifact into the source string, and a
missing ``display_name``/``definition`` key must NOT raise — both paths rely on
safe ``.get`` access. This module has NO imports and does no I/O: it only builds
the source STRING handed to the embedding provider, keeping it a leaf module so
neither peer can form an import cycle through it.
"""


def embed_text(payload: dict) -> str:
    """Build the metric intent-embedding source string from a definition payload.

    Joins ``display_name | definition | *synonyms`` with `` | ``, filtering empty
    parts so an empty ``definition`` does not produce a double-bar artifact. Uses
    safe ``.get`` access so a missing key never raises.
    """
    parts = [payload.get("display_name", ""), payload.get("definition", "")]
    parts.extend(payload.get("synonyms") or [])
    return " | ".join(p for p in parts if p)
