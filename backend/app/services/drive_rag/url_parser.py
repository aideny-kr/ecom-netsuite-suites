"""Parse Drive folder and file IDs from URLs or raw ID strings."""

from __future__ import annotations

import re

_FOLDER_URL_RE = re.compile(r"/folders/([a-zA-Z0-9_-]+)")
_FILE_URL_RE = re.compile(r"/(?:document|spreadsheets|file|presentation)/d/([a-zA-Z0-9_-]+)")
_RAW_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{10,}$")


def parse_folder_id(url_or_id: str) -> str:
    if not url_or_id or not url_or_id.strip():
        raise ValueError("folder URL or ID is required")
    s = url_or_id.strip().rstrip("/")
    m = _FOLDER_URL_RE.search(s)
    if m:
        return m.group(1)
    if _RAW_ID_RE.match(s):
        return s
    raise ValueError(f"could not extract folder ID from: {url_or_id!r}")


def parse_file_id(url_or_id: str) -> str:
    if not url_or_id or not url_or_id.strip():
        raise ValueError("file URL or ID is required")
    s = url_or_id.strip().rstrip("/")
    m = _FILE_URL_RE.search(s)
    if m:
        return m.group(1)
    if _RAW_ID_RE.match(s):
        return s
    raise ValueError(f"could not extract file ID from: {url_or_id!r}")
