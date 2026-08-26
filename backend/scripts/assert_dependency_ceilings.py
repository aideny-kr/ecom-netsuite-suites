"""Fail the build if an INSTALLED dependency falls outside its supported range.

Run immediately after `pip install` in every image that builds this backend.
Both `backend/Dockerfile` and `backend/Dockerfile.prod` invoke it; it exists as
a file rather than an inline `python -c` so the two images cannot drift — the
previous shape had the same assertion pasted into both, and a review round
flagged that the next edit would land in one and miss the other.

WHY THIS DUPLICATES pyproject.toml ON PURPOSE
A guard derived from the manifest cannot catch the manifest failing to apply.
The whole value here is an INDEPENDENT statement of what we expect to be
installed, checked against what actually IS installed. So the ranges below are
written out here deliberately, and yes, that means two places must agree — the
disagreement is the signal. Reading the ranges out of pyproject.toml would make
this a tautology that passes whenever the resolver is skipped entirely.

A digest check cannot substitute for this either: it proves WHICH image runs,
never what pip resolved inside it.

WHY `importlib.metadata` AND NOT `import <pkg>`
Distribution name != import name for several of our dependencies
(`python-jose` -> `jose`, `beautifulsoup4` -> `bs4`, `google-genai` ->
`google.genai`). Metadata lookup keeps this table keyed the same way
pyproject.toml is keyed, so adding an entry needs no per-package knowledge.

Extending the table to every ceiling in pyproject.toml is tracked as ClickUp
86bbmwwmt.
"""

from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError, version

# distribution name -> (minimum inclusive, maximum EXCLUSIVE), as version tuples.
#
# anthropic: 1.x vendors its own httpx line (httpx2/httpcore2), so the
# `httpx.Timeout` our adapters construct is a foreign object to it and reaches
# `anyio.fail_after`, which does `current_time() + delay`:
#     TypeError: unsupported operand type(s) for +: 'float' and 'Timeout'
# Every outbound LLM call dies, and it surfaces to users as the chat's generic
# "I wasn't able to find relevant information" — silent at exactly the layer
# someone would investigate first. See ClickUp 86bbm719c before raising it.
SUPPORTED: dict[str, tuple[tuple[int, ...], tuple[int, ...]]] = {
    "anthropic": ((0, 40), (1, 0)),
}


def _parse(raw: str) -> tuple[int, ...]:
    """Numeric release tuple, ignoring any pre/post/dev suffix.

    `0.40.0rc1` -> (0, 40, 0). Suffixes are dropped rather than ordered
    properly: this is a coarse range check, and a release candidate inside the
    supported range is not what this guard exists to catch.
    """
    parts: list[int] = []
    for chunk in raw.split("."):
        digits = ""
        for ch in chunk:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def violations() -> list[str]:
    """Every installed package outside its supported range, as readable lines."""
    problems: list[str] = []
    for dist, (low, high) in sorted(SUPPORTED.items()):
        try:
            installed_raw = version(dist)
        except PackageNotFoundError:
            problems.append(f"{dist}: declared in pyproject.toml but NOT INSTALLED in this image")
            continue

        installed = _parse(installed_raw)
        if not installed:
            problems.append(f"{dist}: installed version {installed_raw!r} is unparseable")
            continue

        low_s = ".".join(str(n) for n in low)
        high_s = ".".join(str(n) for n in high)
        if installed < low:
            problems.append(f"{dist}: {installed_raw} installed, below the supported floor >={low_s}")
        elif installed >= high:
            problems.append(f"{dist}: {installed_raw} installed, at or above the ceiling <{high_s}")
    return problems


def main() -> int:
    problems = violations()
    if not problems:
        return 0

    print("FATAL: installed dependencies do not match the supported ranges.", file=sys.stderr)
    for line in problems:
        print(f"  - {line}", file=sys.stderr)
    print(
        "\nCheck FIRST that the branch being built actually carries the ceiling in\n"
        "backend/pyproject.toml — on 2026-08-26 the ceiling existed only on a feature\n"
        "branch while main still resolved anthropic to 1.0.0. If the ceiling IS present,\n"
        "rebuild with --no-cache. See ClickUp 86bbm719c before raising the anthropic ceiling.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
