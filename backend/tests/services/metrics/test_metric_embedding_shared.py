"""Pure-unit tests for the SHARED metric intent-embedding source-string builder.

These tests deliberately use NO DB fixture — they exercise only the import-time
module objects and the pure `embed_text` helper. They prove:

- Test A: the seeder and authoring modules bind ONE shared `_embed_text` object
  (the core DRY guarantee — structurally prevents future re-divergence).
- Test B: the empty-definition divergence (seeder's old double-bar artifact) is
  closed — both paths now produce the filtered string.
- Test C: the canonical join/filter contract (safe-get, filter-empties, None
  synonyms) — proving the seeder's old bracket-access KeyError is gone.
- Test D: every current `_SYSTEM_METRICS` payload embeds IDENTICALLY through the
  shared helper vs the pre-fix non-filtered join — so no seeded vector goes stale.
"""

import app.services.metrics.metric_authoring as auth_mod
import app.services.metrics.metric_catalog_seeder as seeder_mod
from app.services.metrics._embedding import embed_text
from app.services.metrics.metric_catalog_seeder import _SYSTEM_METRICS


def test_seeder_and_authoring_share_one_embed_text():
    # Test A — parity by object identity. The two module-level `_embed_text`
    # names must point at the SAME function object (the shared canonical helper),
    # so a future edit cannot silently fork one path without failing this test.
    assert seeder_mod._embed_text is auth_mod._embed_text
    assert seeder_mod._embed_text is embed_text


def test_empty_definition_parity_no_double_bar():
    # Test B — proves the divergence is closed. Empty definition must be filtered,
    # not retained as a double-bar artifact, in BOTH paths.
    payload = {"display_name": "Cash", "definition": "", "synonyms": ["cash balance"]}
    seeder_out = seeder_mod._embed_text(payload)
    auth_out = auth_mod._embed_text(payload)
    assert seeder_out == auth_out
    assert seeder_out == "Cash | cash balance"


def test_canonical_join_and_filter_contract():
    # Test C — canonical join/filter contract.
    assert (
        embed_text(
            {
                "display_name": "Net Margin",
                "definition": "Net income / gross revenue.",
                "synonyms": ["net profit margin", "bottom line margin"],
            }
        )
        == "Net Margin | Net income / gross revenue. | net profit margin | bottom line margin"
    )
    # Empty definition + empty synonyms collapse to just the display name.
    assert embed_text({"display_name": "X", "definition": "", "synonyms": []}) == "X"
    # All-empty payload yields '' (not ' |  | ').
    assert embed_text({"display_name": "", "definition": "", "synonyms": []}) == ""
    # Missing 'definition' key + None synonyms must NOT raise (safe-get replaced
    # the seeder's bracket access that empirically KeyError'd).
    assert embed_text({"display_name": "A", "synonyms": None}) == "A"


def test_no_regression_for_current_system_seeds():
    # Test D — every currently-seeded payload embeds identically through the shared
    # helper vs the pre-fix non-filtered join. All 9 seeds are fully populated, so
    # filter-empties produces the SAME string → no seeded vector becomes stale.
    for m in _SYSTEM_METRICS:
        pre_fix = " | ".join([m["display_name"], m["definition"], *m.get("synonyms", [])])
        assert seeder_mod._embed_text(m) == pre_fix
