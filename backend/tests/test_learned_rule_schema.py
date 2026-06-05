"""Learned-rule schema bounds — rule_description must be capped so an admin can't
store a multi-KB rule that bloats every chat prompt / breaks the prompt cache."""

import pytest
from pydantic import ValidationError

from app.schemas.learned_rule import LearnedRuleCreate, LearnedRuleUpdate

MAX = 4000


def test_create_rejects_overlong_description():
    with pytest.raises(ValidationError):
        LearnedRuleCreate(rule_description="x" * (MAX + 1))


def test_create_accepts_normal_description():
    obj = LearnedRuleCreate(rule_description="Count laptops via item.class")
    assert obj.rule_description.startswith("Count laptops")


def test_update_rejects_overlong_description():
    with pytest.raises(ValidationError):
        LearnedRuleUpdate(rule_description="x" * (MAX + 1))
