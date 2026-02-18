"""Tests for Prompt Template Service — ~10 tests."""

from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.chat.prompts import AGENTIC_SYSTEM_PROMPT
from app.services.prompt_template_service import generate_template, get_active_template
from tests.conftest import create_test_tenant, create_test_user

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def tenant(db: AsyncSession):
    return await create_test_tenant(db, name="Template Corp", plan="pro")


@pytest_asyncio.fixture
async def user(db: AsyncSession, tenant):
    user, _ = await create_test_user(db, tenant, role_name="admin")
    return user


def _make_profile(**kwargs):
    """Create a profile-like object for unit tests (not a real SQLAlchemy model)."""
    return SimpleNamespace(
        industry=kwargs.get("industry"),
        business_description=kwargs.get("business_description"),
        netsuite_account_id=kwargs.get("netsuite_account_id"),
        chart_of_accounts=kwargs.get("chart_of_accounts"),
        subsidiaries=kwargs.get("subsidiaries"),
        item_types=kwargs.get("item_types"),
        custom_segments=kwargs.get("custom_segments"),
        fiscal_calendar=kwargs.get("fiscal_calendar"),
        suiteql_naming=kwargs.get("suiteql_naming"),
    )


def _make_policy(**kwargs):
    """Create a policy-like object for unit tests (not a real SQLAlchemy model)."""
    return SimpleNamespace(
        read_only_mode=kwargs.get("read_only_mode", True),
        allowed_record_types=kwargs.get("allowed_record_types"),
        blocked_fields=kwargs.get("blocked_fields"),
        max_rows_per_query=kwargs.get("max_rows_per_query", 1000),
        require_row_limit=kwargs.get("require_row_limit", True),
        custom_rules=kwargs.get("custom_rules"),
    )


# ---------------------------------------------------------------------------
# Template Generation (unit tests)
# ---------------------------------------------------------------------------


def test_generate_template_has_all_sections():
    profile = _make_profile(industry="Retail", business_description="Online store")
    text, sections = generate_template(profile)

    expected_sections = [
        "identity",
        "netsuite_context",
        "suiteql_rules",
        "tool_rules",
        "policy_constraints",
        "response_rules",
    ]
    for section in expected_sections:
        assert section in sections


def test_template_includes_industry_and_business():
    profile = _make_profile(industry="Fashion", business_description="Luxury clothing brand")
    text, sections = generate_template(profile)

    assert "Fashion" in text
    assert "Luxury clothing brand" in text


def test_template_includes_chart_of_accounts():
    profile = _make_profile(
        chart_of_accounts=[
            {"number": "1000", "name": "Cash"},
            {"number": "2000", "name": "Accounts Receivable"},
        ]
    )
    text, sections = generate_template(profile)

    assert "1000" in text
    assert "Cash" in text


def test_template_includes_naming_conventions():
    profile = _make_profile(suiteql_naming={"transaction_type_field": "type", "date_field": "trandate"})
    text, sections = generate_template(profile)

    assert "transaction_type_field" in text
    assert "trandate" in text


def test_template_with_policy_constraints():
    profile = _make_profile(industry="Finance")
    policy = _make_policy(
        read_only_mode=True,
        blocked_fields=["salary", "ssn"],
        max_rows_per_query=500,
    )
    text, sections = generate_template(profile, policy)

    assert "READ-ONLY MODE" in text
    assert "salary" in text
    assert "ssn" in text
    assert "500" in text


def test_template_without_policy_has_default_readonly():
    profile = _make_profile(industry="Tech")
    text, sections = generate_template(profile)

    assert "read-only" in text.lower()


def test_template_with_custom_rules():
    profile = _make_profile(industry="Healthcare")
    policy = _make_policy(custom_rules=["Never expose patient data", "Limit to aggregate queries"])
    text, sections = generate_template(profile, policy)

    assert "Never expose patient data" in text
    assert "Limit to aggregate queries" in text


# ---------------------------------------------------------------------------
# Fallback to AGENTIC_SYSTEM_PROMPT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_when_no_profile(db: AsyncSession, tenant):
    """When no template exists, fall back to AGENTIC_SYSTEM_PROMPT."""
    result = await get_active_template(db, tenant.id)
    assert result == AGENTIC_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_idempotency_same_profile_same_template():
    profile = _make_profile(
        industry="Retail",
        business_description="Online store",
        netsuite_account_id="12345",
    )
    text1, sections1 = generate_template(profile)
    text2, sections2 = generate_template(profile)

    assert text1 == text2
    assert sections1 == sections2


def test_template_includes_subsidiaries():
    profile = _make_profile(subsidiaries=[{"name": "US Operations"}, {"name": "EU Operations"}])
    text, sections = generate_template(profile)
    assert "US Operations" in text
    assert "EU Operations" in text
