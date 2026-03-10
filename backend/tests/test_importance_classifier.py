import pytest
from app.services.importance_classifier import classify_importance, ImportanceTier


def test_casual_lookup():
    tier = classify_importance("how many orders today")
    assert tier == ImportanceTier.CASUAL


def test_operational_query():
    tier = classify_importance("show me unfulfilled orders by vendor")
    assert tier == ImportanceTier.OPERATIONAL


def test_reporting_grade():
    tier = classify_importance("total revenue by month this quarter")
    assert tier == ImportanceTier.REPORTING


def test_audit_critical_revenue():
    tier = classify_importance("net income by account for Q4 audit")
    assert tier == ImportanceTier.AUDIT_CRITICAL


def test_audit_critical_pl():
    tier = classify_importance("P&L by department for board presentation")
    assert tier == ImportanceTier.AUDIT_CRITICAL


def test_reporting_grade_dashboard():
    tier = classify_importance("sales summary for the monthly dashboard")
    assert tier == ImportanceTier.REPORTING


def test_financial_report_defaults_to_reporting():
    """FINANCIAL_REPORT intent should bump tier to at least REPORTING."""
    tier = classify_importance("show me the numbers", intent_hint="financial_report")
    assert tier.value >= ImportanceTier.REPORTING.value


def test_casual_is_default():
    tier = classify_importance("hello there")
    assert tier == ImportanceTier.CASUAL


def test_audit_keywords_case_insensitive():
    tier = classify_importance("AUDIT the revenue accounts")
    assert tier == ImportanceTier.AUDIT_CRITICAL
