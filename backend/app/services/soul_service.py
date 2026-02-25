import os
import re
import uuid

from app.core.config import settings
from app.schemas.soul import SoulConfigResponse, SoulUpdateRequest


def get_soul_file_path(tenant_id: uuid.UUID) -> str:
    """Get the absolute path to the tenant's soul.md file."""
    storage_dir = getattr(settings, "WORKSPACE_STORAGE_DIR", "/tmp/workspace_storage")
    tenant_dir = os.path.join(storage_dir, str(tenant_id))
    return os.path.join(tenant_dir, "soul.md")


async def get_soul_config(tenant_id: uuid.UUID) -> SoulConfigResponse:
    """Read and parse the tenant's soul.md file if it exists."""
    soul_path = get_soul_file_path(tenant_id)

    if not os.path.exists(soul_path):
        return SoulConfigResponse(bot_tone=None, netsuite_quirks=None, exists=False)

    with open(soul_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Simple regex parsing based on markdown headers.
    # Expecting:
    # # AI Tone
    # <bot tone content>
    # # NetSuite Quirks
    # <netsuite quirks content>

    bot_tone = None
    quirks = None

    # Extract Tone
    tone_match = re.search(r"# AI Tone\s*\n(.*?)(?=\n# |\Z)", content, re.DOTALL | re.IGNORECASE)
    if tone_match:
        bot_tone = tone_match.group(1).strip()

    # Extract Quirks
    quirks_match = re.search(r"# NetSuite Quirks\s*\n(.*?)(?=\n# |\Z)", content, re.DOTALL | re.IGNORECASE)
    if quirks_match:
        quirks = quirks_match.group(1).strip()

    return SoulConfigResponse(
        bot_tone=bot_tone or None,
        netsuite_quirks=quirks or None,
        exists=True
    )


async def update_soul_config(tenant_id: uuid.UUID, data: SoulUpdateRequest) -> SoulConfigResponse:
    """Write the tone and quirks into the tenant's soul.md file."""
    storage_dir = getattr(settings, "WORKSPACE_STORAGE_DIR", "/tmp/workspace_storage")
    tenant_dir = os.path.join(storage_dir, str(tenant_id))
    os.makedirs(tenant_dir, exist_ok=True)

    soul_path = os.path.join(tenant_dir, "soul.md")

    content_parts = []

    # We construct the markdown file based on what's provided.
    if data.bot_tone and data.bot_tone.strip():
        content_parts.append(f"# AI Tone\n\n{data.bot_tone.strip()}\n")

    if data.netsuite_quirks and data.netsuite_quirks.strip():
        content_parts.append(f"# NetSuite Quirks\n\n{data.netsuite_quirks.strip()}\n")

    final_content = "\n".join(content_parts)

    if final_content.strip():
        with open(soul_path, "w", encoding="utf-8") as f:
            f.write(final_content)
        exists = True
    else:
        # If everything is empty, we remove the file
        if os.path.exists(soul_path):
            os.remove(soul_path)
        exists = False

    return SoulConfigResponse(
        bot_tone=data.bot_tone.strip() if data.bot_tone else None,
        netsuite_quirks=data.netsuite_quirks.strip() if data.netsuite_quirks else None,
        exists=exists
    )
