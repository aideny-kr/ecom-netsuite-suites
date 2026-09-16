"""G1(e): the system-record deny-list compares case-normalized.

The model supplies ``recordType`` verbatim. NetSuite REST record types are camelCase and
the deny-list is lowercase, so ``Employee`` or ``ACCOUNT`` used to pass an exact-string
``not in`` check. A repair that only changed casing could route around the list.
"""

import pytest

from app.services.chat.mutation_guard import _BLOCKED_RECORD_TYPES, is_record_type_allowed


@pytest.mark.parametrize("blocked", sorted(_BLOCKED_RECORD_TYPES))
@pytest.mark.parametrize("variant", [str.lower, str.upper, str.title, lambda s: f"  {s}  "])
def test_every_blocked_type_is_refused_in_any_casing_or_padding(blocked, variant):
    assert is_record_type_allowed(variant(blocked)) is False


@pytest.mark.parametrize("allowed", ["customer", "Customer", "CUSTOMER", "creditMemo", "salesOrder", "invoice"])
def test_ordinary_record_types_stay_allowed(allowed):
    assert is_record_type_allowed(allowed) is True


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_missing_record_type_is_not_allowed(empty):
    assert is_record_type_allowed(empty) is False
