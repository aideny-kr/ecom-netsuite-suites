"""Tests for HookManager lifecycle hooks."""

import pytest

from app.services.chat.agents.hooks import HookManager


class TestHookManager:

    def test_fire_with_no_hooks_returns_input_unchanged(self):
        hm = HookManager()
        result = hm.fire_sync("pre_execute", task="hello", context={})
        assert result == {"task": "hello", "context": {}}

    def test_register_and_fire_single_hook(self):
        hm = HookManager()

        def uppercase_task(task: str, context: dict) -> dict:
            return {"task": task.upper(), "context": context}

        hm.register("pre_execute", uppercase_task)
        result = hm.fire_sync("pre_execute", task="hello", context={})
        assert result["task"] == "HELLO"

    def test_fire_chains_multiple_hooks(self):
        hm = HookManager()

        def add_prefix(task: str, context: dict) -> dict:
            return {"task": f"PREFIX_{task}", "context": context}

        def add_suffix(task: str, context: dict) -> dict:
            return {"task": f"{task}_SUFFIX", "context": context}

        hm.register("pre_execute", add_prefix)
        hm.register("pre_execute", add_suffix)
        result = hm.fire_sync("pre_execute", task="hello", context={})
        assert result["task"] == "PREFIX_hello_SUFFIX"

    def test_post_tool_hook_transforms_result(self):
        hm = HookManager()

        def append_processed(tool_name: str, tool_input: dict, tool_result: str) -> str:
            return tool_result + " [processed]"

        hm.register("post_tool", append_processed)
        result = hm.fire_sync("post_tool", tool_name="test", tool_input={}, tool_result="data")
        assert result == "data [processed]"

    def test_pre_response_hook_strips_tags(self):
        hm = HookManager()

        def strip_internal(response_text: str) -> str:
            import re
            return re.sub(r"<internal>.*?</internal>", "", response_text).strip()

        hm.register("pre_response", strip_internal)
        result = hm.fire_sync("pre_response", response_text="Hello <internal>secret</internal> world")
        assert result == "Hello  world"

    def test_on_error_hook_returns_fallback(self):
        hm = HookManager()

        def fallback_handler(error: Exception, context: dict) -> str | None:
            return "fallback response"

        hm.register("on_error", fallback_handler)
        result = hm.fire_sync("on_error", error=ValueError("test"), context={})
        assert result == "fallback response"

    def test_on_error_hook_returns_none_to_escalate(self):
        hm = HookManager()

        def escalate_handler(error: Exception, context: dict) -> str | None:
            return None

        hm.register("on_error", escalate_handler)
        result = hm.fire_sync("on_error", error=ValueError("test"), context={})
        assert result is None

    def test_load_hooks_from_module_path(self, tmp_path):
        """Load hooks from a Python module dynamically."""
        # Create a temporary hooks module
        hooks_file = tmp_path / "test_hooks_module.py"
        hooks_file.write_text(
            "def hook_pre_execute(task: str, context: dict) -> dict:\n"
            "    return {'task': task.upper(), 'context': context}\n"
        )
        import sys
        sys.path.insert(0, str(tmp_path))
        try:
            hm = HookManager()
            hm.load_from_module("test_hooks_module")
            result = hm.fire_sync("pre_execute", task="hello", context={})
            assert result["task"] == "HELLO"
        finally:
            sys.path.pop(0)
            sys.modules.pop("test_hooks_module", None)

    def test_fire_unknown_event_passes_through(self):
        hm = HookManager()
        # Should not raise, returns kwargs as-is
        result = hm.fire_sync("nonexistent_event", task="hello", context={})
        assert result == {"task": "hello", "context": {}}


class TestHookManagerAsync:

    @pytest.mark.asyncio
    async def test_async_fire_pre_execute(self):
        hm = HookManager()

        async def async_hook(task: str, context: dict) -> dict:
            return {"task": task.upper(), "context": context}

        hm.register("pre_execute", async_hook)
        result = await hm.fire("pre_execute", task="hello", context={})
        assert result["task"] == "HELLO"
