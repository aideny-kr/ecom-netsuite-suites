"""Tests for chat orchestrator — agentic loop with mocked Claude API."""

import uuid

from app.services.chat.nodes import (
    ALLOWED_CHAT_TOOLS,
    OrchestratorState,
)


def _make_state(**overrides) -> OrchestratorState:
    """Create a minimal OrchestratorState for testing."""
    defaults = {
        "user_message": "What are my recent orders?",
        "tenant_id": uuid.uuid4(),
        "actor_id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
    }
    defaults.update(overrides)
    return OrchestratorState(**defaults)


class TestAllowedChatToolsFromOld:
    """Verify ALLOWED_CHAT_TOOLS is still correct after refactor."""

    def test_is_frozenset(self):
        assert isinstance(ALLOWED_CHAT_TOOLS, frozenset)

    def test_contains_only_read_tools(self):
        expected = {
            "netsuite.suiteql",
            "netsuite.connectivity",
            "data.sample_table_read",
            "report.export",
            "workspace.list_files",
            "workspace.read_file",
            "workspace.search",
            "workspace.propose_patch",
        }
        assert ALLOWED_CHAT_TOOLS == expected
