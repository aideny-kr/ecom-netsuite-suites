"""Tests for the disclosure module: DisclosureBlock dataclass, WHERE-clause parser,
query classifier, can_switch_source logic, and assemble_disclosure hook.
"""

from __future__ import annotations


class TestDisclosureBlock:
    def test_defaults(self):
        from app.services.chat.disclosure import DisclosureBlock

        block = DisclosureBlock(source="netsuite", interpretation="")
        assert block.source == "netsuite"
        assert block.interpretation == ""
        assert block.implicit_filters == []
        assert block.can_switch_source is False
        assert block.is_rerun is False
        assert block.failure_mode is False

    def test_to_dict_full(self):
        from app.services.chat.disclosure import DisclosureBlock

        block = DisclosureBlock(
            source="bigquery",
            interpretation='"This week" = Mon Apr 5 - today',
            implicit_filters=["Excludes cancelled orders", "Excludes test orders"],
            can_switch_source=True,
            is_rerun=True,
            failure_mode=False,
        )
        d = block.to_dict()
        assert d == {
            "source": "bigquery",
            "interpretation": '"This week" = Mon Apr 5 - today',
            "implicit_filters": ["Excludes cancelled orders", "Excludes test orders"],
            "can_switch_source": True,
            "is_rerun": True,
            "failure_mode": False,
        }

    def test_to_dict_defaults(self):
        from app.services.chat.disclosure import DisclosureBlock

        block = DisclosureBlock(source="netsuite", interpretation="x")
        d = block.to_dict()
        assert d["source"] == "netsuite"
        assert d["interpretation"] == "x"
        assert d["implicit_filters"] == []
        assert d["can_switch_source"] is False
        assert d["is_rerun"] is False
        assert d["failure_mode"] is False

    def test_source_literal_type(self):
        """Source must be literal netsuite | bigquery."""
        from app.services.chat.disclosure import DisclosureBlock

        # Valid
        DisclosureBlock(source="netsuite", interpretation="")
        DisclosureBlock(source="bigquery", interpretation="")
