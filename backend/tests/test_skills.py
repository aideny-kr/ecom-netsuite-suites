"""Tests for the Agent Skills registry — loading, parsing, trigger matching, prompt injection, and API."""

import uuid

import pytest
from sqlalchemy import text

from app.services.chat.skills import (
    get_all_skills_metadata,
    get_skill_by_trigger,
    get_skill_instructions,
    match_skill,
)


class TestSkillLoading:
    def test_loads_all_skills(self):
        """All SKILL.md files should be discovered and parsed."""
        skills = get_all_skills_metadata()
        assert len(skills) >= 4
        names = {s["name"] for s in skills}
        assert "NetSuite CSV Import Template Generator" in names
        assert "Sales by Platform Analysis" in names
        assert "Period-over-Period Comparison" in names
        assert "Inventory Availability Check" in names

    def test_metadata_has_required_fields(self):
        """Each skill metadata should have name, description, triggers."""
        skills = get_all_skills_metadata()
        for skill in skills:
            assert "name" in skill, f"Missing 'name' in {skill}"
            assert "description" in skill, f"Missing 'description' in {skill}"
            assert "triggers" in skill, f"Missing 'triggers' in {skill}"
            assert "slug" in skill, f"Missing 'slug' in {skill}"
            assert isinstance(skill["triggers"], list)
            assert len(skill["triggers"]) > 0

    def test_slug_is_directory_name(self):
        """Slug should be the directory name of the skill."""
        skills = get_all_skills_metadata()
        slugs = {s["slug"] for s in skills}
        assert "csv_import_generator" in slugs
        assert "sales_by_platform" in slugs


class TestTriggerMatching:
    def test_exact_slash_command(self):
        """Exact slash command should match."""
        skill = get_skill_by_trigger("/csv-template")
        assert skill is not None
        assert skill["name"] == "NetSuite CSV Import Template Generator"

    def test_slash_command_with_args(self):
        """/csv-template Sales Order should match."""
        skill = match_skill("/csv-template Sales Order")
        assert skill is not None
        assert skill["name"] == "NetSuite CSV Import Template Generator"

    def test_semantic_trigger(self):
        """Natural language triggers should match."""
        skill = match_skill("create a csv import template")
        assert skill is not None
        assert skill["name"] == "NetSuite CSV Import Template Generator"

    def test_semantic_trigger_case_insensitive(self):
        """Trigger matching should be case-insensitive."""
        skill = match_skill("Create A CSV Import Template")
        assert skill is not None

    def test_partial_semantic_match(self):
        """Should match when user message contains a trigger phrase."""
        skill = match_skill("Can you create a csv import template for customers?")
        assert skill is not None
        assert skill["name"] == "NetSuite CSV Import Template Generator"

    def test_no_match(self):
        """Non-matching input should return None."""
        skill = match_skill("What is the weather today?")
        assert skill is None

    def test_inventory_trigger(self):
        skill = match_skill("/inventory")
        assert skill is not None
        assert skill["name"] == "Inventory Availability Check"

    def test_period_compare_trigger(self):
        skill = match_skill("compare sales month over month")
        assert skill is not None
        assert skill["name"] == "Period-over-Period Comparison"

    def test_sales_platform_trigger(self):
        skill = match_skill("/sales-by-platform")
        assert skill is not None
        assert skill["name"] == "Sales by Platform Analysis"


class TestSkillInstructions:
    def test_get_full_instructions(self):
        """Should return the full markdown body (without frontmatter)."""
        instructions = get_skill_instructions("csv_import_generator")
        assert instructions is not None
        assert "NetSuite CSV Import Template Generator" in instructions
        assert "Identify the Record Type" in instructions
        # Should NOT contain YAML frontmatter delimiters
        assert instructions.strip().startswith("#")

    def test_get_instructions_unknown_skill(self):
        """Unknown skill slug should return None."""
        instructions = get_skill_instructions("nonexistent_skill")
        assert instructions is None

    def test_instructions_exclude_frontmatter(self):
        """Instructions should not include the YAML frontmatter block."""
        instructions = get_skill_instructions("sales_by_platform")
        assert instructions is not None
        assert "Triggers:" not in instructions
        assert "---" not in instructions.split("\n")[0]


# ---------------------------------------------------------------------------
# Unified agent prompt injection tests
# ---------------------------------------------------------------------------


class TestSkillPromptInjection:
    def test_active_skill_injected_into_prompt(self):
        """When a skill is triggered, its full instructions appear in the prompt."""
        from app.services.chat.agents.unified_agent import UnifiedAgent

        agent = UnifiedAgent(
            tenant_id=uuid.UUID("bf92d059-0000-0000-0000-000000000000"),
            user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            correlation_id="test",
        )
        agent._active_skill = {"slug": "csv_import_generator", "name": "test"}

        prompt = agent.system_prompt
        assert "<skill_instructions>" in prompt
        assert "Identify the Record Type" in prompt
        assert "Follow the instructions above step-by-step" in prompt

    def test_no_active_skill_shows_available_skills(self):
        """When no skill is active, available skills are listed."""
        from app.services.chat.agents.unified_agent import UnifiedAgent

        agent = UnifiedAgent(
            tenant_id=uuid.UUID("bf92d059-0000-0000-0000-000000000000"),
            user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            correlation_id="test",
        )
        agent._active_skill = None

        prompt = agent.system_prompt
        assert "<available_skills>" in prompt
        assert "/csv-template" in prompt
        assert "/sales-by-platform" in prompt
        assert "/inventory" in prompt

    def test_active_skill_excludes_available_list(self):
        """When a skill is active, the available skills list should NOT appear."""
        from app.services.chat.agents.unified_agent import UnifiedAgent

        agent = UnifiedAgent(
            tenant_id=uuid.UUID("bf92d059-0000-0000-0000-000000000000"),
            user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            correlation_id="test",
        )
        agent._active_skill = {"slug": "sales_by_platform", "name": "test"}

        prompt = agent.system_prompt
        assert "<skill_instructions>" in prompt
        assert "<available_skills>" not in prompt


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


class TestSkillsCatalogAPI:
    @pytest.mark.asyncio
    async def test_list_agent_skills(self, client, db, admin_user):
        """GET /api/v1/skills/catalog → 200 with skill metadata."""
        _, headers = admin_user

        resp = await client.get("/api/v1/skills/catalog", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 4

        names = {s["name"] for s in data}
        assert "NetSuite CSV Import Template Generator" in names
        assert "Sales by Platform Analysis" in names

        # Each skill should have required fields
        for skill in data:
            assert "name" in skill
            assert "description" in skill
            assert "triggers" in skill
            assert "slug" in skill
            assert isinstance(skill["triggers"], list)

    @pytest.mark.asyncio
    async def test_list_agent_skills_unauthenticated(self, client):
        """GET /api/v1/skills/catalog without auth → 401/403."""
        resp = await client.get("/api/v1/skills/catalog")
        assert resp.status_code in (401, 403)
