"""Guard the netsuite.yaml address-query rules — the soft 'both work' guidance let
the agent filter with BUILTIN.DF (a per-row function) on an unbounded address join,
which timed out (2026-06). These assert the strengthened rules stay in the profile.
"""

from pathlib import Path

import app.services.chat.knowledge_profiles as kp


def _netsuite_yaml() -> str:
    return (Path(kp.__file__).parent / "netsuite.yaml").read_text()


def test_forbids_builtin_df_in_where_for_filtering():
    text = _netsuite_yaml()
    assert "NEVER `BUILTIN.DF(...)` in a WHERE" in text, "address rules must forbid BUILTIN.DF in WHERE (perf killer)"


def test_translates_country_names_to_iso():
    text = _netsuite_yaml()
    assert "Translate user country NAMES to ISO-2" in text


def test_requires_date_scope_on_address_joins():
    text = _netsuite_yaml()
    assert "Address-table joins are HEAVY" in text
    assert "t.trandate" in text
    # the denormalized escape hatch is documented
    assert "t.shipcountry" in text
