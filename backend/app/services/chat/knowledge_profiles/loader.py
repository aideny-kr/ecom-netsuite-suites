from __future__ import annotations

import fnmatch
import logging
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class KnowledgeProfile(BaseModel):
    profile_id: str
    display_name: str
    trigger_tools: list[str] = Field(default_factory=list)
    prompt_fragment: str = ""
    rag_partitions: list[str] = Field(default_factory=list)

    def matches_tools(self, tool_names: set[str]) -> bool:
        for trigger in self.trigger_tools:
            if "*" in trigger or "?" in trigger:
                if any(fnmatch.fnmatch(name, trigger) for name in tool_names):
                    return True
            elif trigger in tool_names:
                return True
        return False


def load_all_profiles(directory: str | Path | None = None) -> list[KnowledgeProfile]:
    if directory is None:
        directory = Path(__file__).parent
    directory = Path(directory)

    if not directory.is_dir():
        return []

    profiles: list[KnowledgeProfile] = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            with open(path) as f:
                data = yaml.safe_load(f)
            if not isinstance(data, dict):
                logger.warning("knowledge_profile.skip_non_dict: %s", path.name)
                continue
            profiles.append(KnowledgeProfile(**data))
        except Exception:
            logger.warning("knowledge_profile.skip_malformed: %s", path.name, exc_info=True)
    return profiles
