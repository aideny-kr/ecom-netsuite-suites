"""Unit tests for the DisclosureBlock dataclass."""

from app.services.chat.disclosure import DisclosureBlock


def test_disclosure_block_defaults():
    block = DisclosureBlock(source="netsuite", interpretation="Read from NetSuite.")
    assert block.source == "netsuite"
    assert block.interpretation == "Read from NetSuite."
    assert block.implicit_filters == []
    assert block.can_switch_source is False
    assert block.is_rerun is False
    assert block.failure_mode is False


def test_disclosure_block_to_dict():
    block = DisclosureBlock(
        source="bigquery",
        interpretation="Read from BigQuery.",
        implicit_filters=["Excludes test orders"],
        can_switch_source=True,
        is_rerun=True,
    )
    d = block.to_dict()
    assert d == {
        "source": "bigquery",
        "interpretation": "Read from BigQuery.",
        "implicit_filters": ["Excludes test orders"],
        "can_switch_source": True,
        "is_rerun": True,
        "failure_mode": False,
    }
