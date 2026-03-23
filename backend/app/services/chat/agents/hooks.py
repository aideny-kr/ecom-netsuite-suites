"""HookManager — lifecycle hooks for specialized agents.

Supports sync (fire_sync) and async (fire) execution of registered hooks.
Hooks are chained: each hook receives the output of the previous one.
"""

from __future__ import annotations

import asyncio
import importlib
from collections import defaultdict
from typing import Any, Callable


class HookManager:
    """Manages lifecycle hooks for agent execution."""

    def __init__(self) -> None:
        self._hooks: dict[str, list[Callable]] = defaultdict(list)

    def register(self, event_name: str, fn: Callable) -> None:
        """Register a hook function for an event."""
        self._hooks[event_name].append(fn)

    def fire_sync(self, event_name: str, **kwargs: Any) -> Any:
        """Fire hooks synchronously, chaining results."""
        hooks = self._hooks.get(event_name, [])
        if not hooks:
            return kwargs

        if event_name == "pre_execute":
            task = kwargs["task"]
            context = kwargs["context"]
            for hook in hooks:
                result = hook(task=task, context=context)
                task = result["task"]
                context = result["context"]
            return {"task": task, "context": context}

        elif event_name == "post_tool":
            tool_name = kwargs["tool_name"]
            tool_input = kwargs["tool_input"]
            tool_result = kwargs["tool_result"]
            for hook in hooks:
                tool_result = hook(tool_name=tool_name, tool_input=tool_input, tool_result=tool_result)
            return tool_result

        elif event_name == "pre_response":
            response_text = kwargs["response_text"]
            for hook in hooks:
                response_text = hook(response_text=response_text)
            return response_text

        elif event_name == "on_error":
            error = kwargs["error"]
            context = kwargs["context"]
            for hook in hooks:
                result = hook(error=error, context=context)
                if result is not None:
                    return result
            return None

        else:
            # Unknown event — pass through kwargs unchanged
            return kwargs

    async def fire(self, event_name: str, **kwargs: Any) -> Any:
        """Fire hooks asynchronously, awaiting coroutines."""
        hooks = self._hooks.get(event_name, [])
        if not hooks:
            return kwargs

        if event_name == "pre_execute":
            task = kwargs["task"]
            context = kwargs["context"]
            for hook in hooks:
                if asyncio.iscoroutinefunction(hook):
                    result = await hook(task=task, context=context)
                else:
                    result = hook(task=task, context=context)
                task = result["task"]
                context = result["context"]
            return {"task": task, "context": context}

        elif event_name == "post_tool":
            tool_name = kwargs["tool_name"]
            tool_input = kwargs["tool_input"]
            tool_result = kwargs["tool_result"]
            for hook in hooks:
                if asyncio.iscoroutinefunction(hook):
                    tool_result = await hook(tool_name=tool_name, tool_input=tool_input, tool_result=tool_result)
                else:
                    tool_result = hook(tool_name=tool_name, tool_input=tool_input, tool_result=tool_result)
            return tool_result

        elif event_name == "pre_response":
            response_text = kwargs["response_text"]
            for hook in hooks:
                if asyncio.iscoroutinefunction(hook):
                    response_text = await hook(response_text=response_text)
                else:
                    response_text = hook(response_text=response_text)
            return response_text

        elif event_name == "on_error":
            error = kwargs["error"]
            context = kwargs["context"]
            for hook in hooks:
                if asyncio.iscoroutinefunction(hook):
                    result = await hook(error=error, context=context)
                else:
                    result = hook(error=error, context=context)
                if result is not None:
                    return result
            return None

        else:
            return kwargs

    def load_from_module(self, module_path: str) -> None:
        """Load hook functions from a Python module.

        Functions named ``hook_<event_name>`` are auto-registered to
        the corresponding event.
        """
        mod = importlib.import_module(module_path)
        for attr_name in dir(mod):
            if not attr_name.startswith("hook_"):
                continue
            fn = getattr(mod, attr_name)
            if callable(fn):
                event_name = attr_name[len("hook_"):]
                self.register(event_name, fn)
