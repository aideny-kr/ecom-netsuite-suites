"""Agent Skills registry — file-system-based skill loader with progressive disclosure.

Skills are defined as SKILL.md files in subdirectories of this package.
Each SKILL.md has YAML frontmatter (Name, Description, Triggers) and a markdown body
containing execution instructions.

On import, all skills are scanned and their metadata cached. Full instructions
are loaded on-demand when a skill is triggered (progressive disclosure).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

_SKILLS_DIR = Path(__file__).parent
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# Cache: populated on first call
_skills_cache: list[dict[str, Any]] | None = None


def _parse_skill_md(path: Path) -> dict[str, Any] | None:
    """Parse a SKILL.md file, returning metadata dict or None on failure."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None

    match = _FRONTMATTER_RE.match(content)
    if not match:
        return None

    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None

    if not isinstance(meta, dict):
        return None

    required = {"Name", "Description", "Triggers"}
    if not required.issubset(meta.keys()):
        return None

    return {
        "name": meta["Name"],
        "description": meta["Description"],
        "triggers": meta["Triggers"],
        "slug": path.parent.name,
        "_path": str(path),
    }


def _load_skills() -> list[dict[str, Any]]:
    """Scan skills directory and load all SKILL.md metadata."""
    skills = []
    for entry in sorted(_SKILLS_DIR.iterdir()):
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        skill_md = entry / "SKILL.md"
        if skill_md.exists():
            parsed = _parse_skill_md(skill_md)
            if parsed:
                skills.append(parsed)
    return skills


def get_all_skills_metadata() -> list[dict[str, Any]]:
    """Return lean metadata for all registered skills (cached)."""
    global _skills_cache
    if _skills_cache is None:
        _skills_cache = _load_skills()
    return _skills_cache


def get_skill_by_trigger(trigger: str) -> dict[str, Any] | None:
    """Find a skill by exact slash-command trigger (e.g., '/csv-template')."""
    trigger_lower = trigger.strip().lower()
    for skill in get_all_skills_metadata():
        for t in skill["triggers"]:
            if t.lower() == trigger_lower:
                return skill
    return None


def match_skill(user_input: str) -> dict[str, Any] | None:
    """Match user input against skill triggers.

    Checks:
    1. Exact slash command match (first word)
    2. Semantic trigger phrase contained in user input
    """
    text = user_input.strip()
    text_lower = text.lower()

    # Check slash commands first (first word)
    first_word = text_lower.split()[0] if text_lower else ""
    if first_word.startswith("/"):
        for skill in get_all_skills_metadata():
            for trigger in skill["triggers"]:
                if trigger.lower() == first_word:
                    return skill

    # Check semantic triggers (phrase contained in input)
    for skill in get_all_skills_metadata():
        for trigger in skill["triggers"]:
            trigger_lower = trigger.lower()
            if trigger_lower.startswith("/"):
                continue  # Skip slash commands for semantic matching
            if trigger_lower in text_lower:
                return skill

    return None


def get_skill_instructions(slug: str) -> str | None:
    """Load full instructions for a skill by slug (progressive disclosure).

    Returns the markdown body without YAML frontmatter, or None if not found.
    """
    for skill in get_all_skills_metadata():
        if skill["slug"] == slug:
            try:
                content = Path(skill["_path"]).read_text(encoding="utf-8")
            except OSError:
                return None

            match = _FRONTMATTER_RE.match(content)
            if match:
                body = content[match.end():]
            else:
                body = content

            return body.strip()

    return None


def reload_skills() -> None:
    """Force reload of skills from disk (useful after adding new skills)."""
    global _skills_cache
    _skills_cache = None
