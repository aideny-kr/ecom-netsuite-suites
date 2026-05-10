"""Deny-by-default mechanical-fix classifier.

Maps Oracle validate codes → deterministic patch generators. ONLY rules in
the allowlist below are auto-fixable; everything else (including OWASP
severity, architectural concerns, governance hot-spots) is narrate-only.

To add a new fixable rule:
1. Append a row to _ALLOWED_RULES with the Oracle code, rule_id (RAG
   citation key), and a `replacement_summary` describing the deterministic
   transform.
2. Wire the actual patch generator in workspace_propose_patch dispatch
   (keyed on rule_id).

Codex #10 explicitly demands deny-by-default — DO NOT add general regex
matching here. Each rule is opt-in.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MechanicalFix:
    rule_id: str
    replacement_summary: str


_ALLOWED_RULES: dict[str, MechanicalFix] = {
    "SUITESCRIPT-DEPRECATED-2X": MechanicalFix(
        rule_id="netsuite-suitescript-upgrade/nlapi-to-n-search",
        replacement_summary=(
            "Replace nlapi* call with the equivalent N/search / N/record API per Oracle migration table."
        ),
    ),
    # Future fixable rules go here. Each must have a deterministic transform
    # (no judgment call, no business-logic guess). Validate by:
    # - "Given this snippet, do all reasonable engineers produce the same fix?"
    # - "Does the transform require knowing user-specific config?"
    # If either fails — keep the rule narrate-only.
}


def classify(*, code: str | None, message: str, file_path: str | None, line: int | None) -> MechanicalFix | None:
    """Return a MechanicalFix iff the rule is in the allowlist; None otherwise."""
    if not code:
        return None
    return _ALLOWED_RULES.get(code)
