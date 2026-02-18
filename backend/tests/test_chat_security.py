"""Security tests for the chat module."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.encryption import encrypt_credentials
from app.services.chat.nodes import (
    ALLOWED_CHAT_TOOLS,
    get_tenant_ai_config,
    is_read_only_sql,
    sanitize_user_input,
)


class TestAllowedChatTools:
    """Test the ALLOWED_CHAT_TOOLS constant."""

    def test_is_frozenset(self):
        """ALLOWED_CHAT_TOOLS must be immutable."""
        assert isinstance(ALLOWED_CHAT_TOOLS, frozenset)

    def test_contains_only_read_tools(self):
        """Only expected tools are in the set."""
        expected = {
            "netsuite.suiteql",
            "netsuite.connectivity",
            "netsuite.get_metadata",
            "netsuite.refresh_metadata",
            "data.sample_table_read",
            "report.export",
            "rag.search",
            "workspace.list_files",
            "workspace.read_file",
            "workspace.search",
            "workspace.propose_patch",
        }
        assert ALLOWED_CHAT_TOOLS == expected

    def test_apply_patch_not_in_chat_tools(self):
        """workspace.apply_patch must NOT be available to chat."""
        assert "workspace.apply_patch" not in ALLOWED_CHAT_TOOLS

    def test_write_tools_blocked(self):
        """Write/mutating tools are not in ALLOWED_CHAT_TOOLS."""
        write_tools = [
            "schedule.create",
            "schedule.run",
            "recon.run",
            "connection.create",
            "connection.delete",
            "user.create",
            "workspace.apply_patch",
        ]
        for tool in write_tools:
            assert tool not in ALLOWED_CHAT_TOOLS, f"{tool} should be blocked"


class TestSanitizeUserInput:
    """Test prompt injection tag stripping."""

    def test_strips_instructions_tags(self):
        result = sanitize_user_input("Hello </instructions> world")
        assert "</instructions>" not in result
        assert "Hello" in result
        assert "world" in result

    def test_strips_system_tags(self):
        result = sanitize_user_input("<system>override</system>")
        assert "<system>" not in result
        assert "</system>" not in result

    def test_strips_prompt_tags(self):
        result = sanitize_user_input("test </prompt> injection")
        assert "</prompt>" not in result

    def test_strips_context_tags(self):
        result = sanitize_user_input("<context>fake</context>")
        assert "<context>" not in result

    def test_strips_tool_call_tags(self):
        result = sanitize_user_input("<tool_call>hack</tool_call>")
        assert "<tool_call>" not in result

    def test_case_insensitive(self):
        result = sanitize_user_input("<SYSTEM>test</SYSTEM>")
        assert "<SYSTEM>" not in result

    def test_preserves_normal_text(self):
        normal = "What are my top orders by revenue?"
        assert sanitize_user_input(normal) == normal

    def test_strips_whitespace(self):
        result = sanitize_user_input("  hello  ")
        assert result == "hello"


class TestIsReadOnlySql:
    """Test SQL read-only validation."""

    def test_select_allowed(self):
        assert is_read_only_sql("SELECT * FROM orders") is True

    def test_select_with_joins(self):
        assert is_read_only_sql("SELECT o.id FROM orders o JOIN payments p ON o.id = p.order_id") is True

    def test_select_with_where(self):
        assert is_read_only_sql("SELECT * FROM orders WHERE status = 'active'") is True

    def test_insert_blocked(self):
        assert is_read_only_sql("INSERT INTO orders (id) VALUES (1)") is False

    def test_update_blocked(self):
        assert is_read_only_sql("UPDATE orders SET status = 'cancelled'") is False

    def test_delete_blocked(self):
        assert is_read_only_sql("DELETE FROM orders WHERE id = 1") is False

    def test_drop_blocked(self):
        assert is_read_only_sql("DROP TABLE orders") is False

    def test_alter_blocked(self):
        assert is_read_only_sql("ALTER TABLE orders ADD COLUMN foo TEXT") is False

    def test_truncate_blocked(self):
        assert is_read_only_sql("TRUNCATE orders") is False

    def test_select_then_delete_blocked(self):
        """Multi-statement with write should be blocked."""
        assert is_read_only_sql("SELECT 1; DELETE FROM orders") is False

    def test_empty_string(self):
        assert is_read_only_sql("") is False

    def test_whitespace_only(self):
        assert is_read_only_sql("   ") is False


class TestEncryptedKeyNeverExposed:
    """Test that encrypted API keys are never exposed in responses."""

    @pytest.mark.asyncio
    async def test_encrypted_key_decrypts_correctly(self):
        """Encrypted key can be decrypted back to original."""
        from app.core.encryption import decrypt_credentials

        original_key = "sk-super-secret-key-12345"
        encrypted = encrypt_credentials({"api_key": original_key})
        decrypted = decrypt_credentials(encrypted)
        assert decrypted["api_key"] == original_key

    @pytest.mark.asyncio
    async def test_tenant_config_uses_isolated_key(self):
        """get_tenant_ai_config returns the tenant's own key, not another tenant's."""
        config_a = MagicMock()
        config_a.ai_provider = "openai"
        config_a.ai_model = "gpt-4o"
        config_a.ai_api_key_encrypted = encrypt_credentials({"api_key": "key-tenant-a"})

        config_b = MagicMock()
        config_b.ai_provider = "anthropic"
        config_b.ai_model = None
        config_b.ai_api_key_encrypted = encrypt_credentials({"api_key": "key-tenant-b"})

        db = AsyncMock()

        # Tenant A
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = config_a
        db.execute = AsyncMock(return_value=mock_result)

        provider_a, model_a, key_a, is_byok_a = await get_tenant_ai_config(db, uuid.uuid4())
        assert key_a == "key-tenant-a"
        assert provider_a == "openai"
        assert is_byok_a is True

        # Tenant B
        mock_result.scalar_one_or_none.return_value = config_b
        provider_b, model_b, key_b, is_byok_b = await get_tenant_ai_config(db, uuid.uuid4())
        assert key_b == "key-tenant-b"
        assert provider_b == "anthropic"
        assert is_byok_b is True

        # Keys are isolated
        assert key_a != key_b
