"""Simple in-process MCP metrics counters for health/debugging."""

import threading
from collections import defaultdict

_lock = threading.Lock()

_calls: dict[tuple[str, str], int] = defaultdict(int)  # (tool_name, status) -> count
_durations: dict[str, list[float]] = defaultdict(list)  # tool_name -> [seconds]
_rate_limit_rejections: dict[str, int] = defaultdict(int)  # tool_name -> count


def record_call(tool_name: str, status: str) -> None:
    with _lock:
        _calls[(tool_name, status)] += 1


def record_duration(tool_name: str, seconds: float) -> None:
    with _lock:
        _durations[tool_name].append(seconds)


def record_rate_limit_rejection(tool_name: str) -> None:
    with _lock:
        _rate_limit_rejections[tool_name] += 1


def get_metrics() -> dict:
    """Return a snapshot of all metrics for health/debugging."""
    with _lock:
        calls_by_tool: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for (tool, status), count in _calls.items():
            calls_by_tool[tool][status] = count

        return {
            "mcp_tool_calls_total": dict(calls_by_tool),
            "mcp_tool_duration_seconds": {
                tool: {"count": len(durs), "total": sum(durs), "avg": sum(durs) / len(durs) if durs else 0}
                for tool, durs in _durations.items()
            },
            "mcp_rate_limit_rejections_total": dict(_rate_limit_rejections),
        }


def reset_metrics() -> None:
    """Reset all counters — used in tests."""
    with _lock:
        _calls.clear()
        _durations.clear()
        _rate_limit_rejections.clear()
