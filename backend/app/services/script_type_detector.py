"""Shared script type detection for SuiteScript files.

Detects script type from:
1. Content — @NScriptType JSDoc annotation (most reliable)
2. Filename — heuristics matching frontend suitescript-parser.ts
3. Fallback — "Other"

Used by:
- workspace_rag_seeder.py (chunking)
- suitescript_sync_service.py (file organization)
- workspace_reorganizer.py (bulk reorganization)
"""

from __future__ import annotations

import re
from typing import Final

# Canonical script types (match frontend ScriptType union)
SCRIPT_TYPES: Final[list[str]] = [
    "UserEventScript",
    "ClientScript",
    "ScheduledScript",
    "MapReduceScript",
    "Suitelet",
    "Restlet",
    "WorkflowActionScript",
    "BundleInstallationScript",
    "MassUpdateScript",
    "Library",
    "Other",
]

# Script type → display folder name
SCRIPT_TYPE_FOLDER_MAP: Final[dict[str, str]] = {
    "UserEventScript": "User Event Scripts",
    "ClientScript": "Client Scripts",
    "ScheduledScript": "Scheduled Scripts",
    "MapReduceScript": "Map Reduce",
    "Suitelet": "Suitelets",
    "Restlet": "RESTlets",
    "WorkflowActionScript": "Workflow Actions",
    "BundleInstallationScript": "Bundle Installation",
    "MassUpdateScript": "Mass Update",
    "Library": "Libraries",
    "Other": "Other",
}

# @NScriptType value → canonical type (case-insensitive lookup)
_ANNOTATION_MAP: Final[dict[str, str]] = {k.lower(): k for k in SCRIPT_TYPES if k not in ("Library", "Other")}

# Filename heuristic patterns — order matters (first match wins)
# Aligned with frontend suitescript-parser.ts fallback logic
_FILENAME_PATTERNS: Final[list[tuple[re.Pattern[str], str]]] = [
    (re.compile(r"userevent|_ue\b", re.IGNORECASE), "UserEventScript"),
    (re.compile(r"client|_cs\b", re.IGNORECASE), "ClientScript"),
    (re.compile(r"scheduled|_ss\b", re.IGNORECASE), "ScheduledScript"),
    (re.compile(r"mapreduce|_mr\b", re.IGNORECASE), "MapReduceScript"),
    (re.compile(r"suitelet|_su\b", re.IGNORECASE), "Suitelet"),
    (re.compile(r"restlet|_rl\b", re.IGNORECASE), "Restlet"),
    (re.compile(r"workflow|_wa\b", re.IGNORECASE), "WorkflowActionScript"),
    (re.compile(r"bundle|_bi\b", re.IGNORECASE), "BundleInstallationScript"),
    (re.compile(r"massupdate|_mu\b", re.IGNORECASE), "MassUpdateScript"),
    (re.compile(r"util|lib|helper", re.IGNORECASE), "Library"),
]


def detect_from_content(content: str) -> str | None:
    """Extract script type from @NScriptType JSDoc annotation.

    Returns canonical type string or None if not found.
    """
    m = re.search(r"@NScriptType\s+(\w+)", content)
    if not m:
        return None
    raw = m.group(1).lower()
    return _ANNOTATION_MAP.get(raw)


def detect_from_filename(filename: str) -> str | None:
    """Infer script type from filename heuristics.

    Returns canonical type string or None if no pattern matches.
    """
    for pattern, script_type in _FILENAME_PATTERNS:
        if pattern.search(filename):
            return script_type
    return None


def resolve_script_type(
    content: str | None = None,
    filename: str = "",
    metadata_type: str | None = None,
) -> str:
    """Resolve script type using priority: content > metadata > filename > "Other".

    Args:
        content: File content (if available)
        filename: Filename or path
        metadata_type: Script type from NetSuite custom script record metadata

    Returns:
        Canonical script type string (never None)
    """
    # Priority 1: @NScriptType from content
    if content:
        from_content = detect_from_content(content)
        if from_content:
            return from_content

    # Priority 2: NetSuite script record metadata
    if metadata_type:
        normalized = metadata_type.lower()
        if normalized in _ANNOTATION_MAP:
            return _ANNOTATION_MAP[normalized]

    # Priority 3: Filename heuristics
    from_filename = detect_from_filename(filename)
    if from_filename:
        return from_filename

    return "Other"


def get_folder_for_type(script_type: str) -> str:
    """Get the display folder name for a script type."""
    return SCRIPT_TYPE_FOLDER_MAP.get(script_type, "Other")
