"""SQLAlchemy stays below 2.1 (2026-09-24).

SQLAlchemy 2.1.0 changed the default driver for a plain ``postgresql://`` URL from
psycopg2 to psycopg (v3), which is not installed. With an unbounded pin, a fresh install
resolved 2.1.0 and ``app/workers/base_task.py``'s ``create_engine(DATABASE_URL_SYNC)``
raised ``ModuleNotFoundError: No module named 'psycopg'`` at import: main's CI failed on
18f38d49, and any backend image built from main would have crashed on start.
"""

from __future__ import annotations

import re
import sys
from importlib.metadata import version
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "scripts"))

import assert_dependency_ceilings as ceilings  # noqa: E402


def test_the_installed_sqlalchemy_is_below_2_1():
    installed = ceilings._parse(version("sqlalchemy"))
    assert installed < (2, 1), f"sqlalchemy {version('sqlalchemy')} installed; 2.1 defaults postgresql:// to psycopg v3"


def test_pyproject_and_the_image_check_agree_on_the_sqlalchemy_ceiling():
    pyproject = (BACKEND / "pyproject.toml").read_text()
    spec = re.search(r'"sqlalchemy\[asyncio\]([^"]*)"', pyproject)
    assert spec and "<2.1" in spec.group(1), spec
    assert ceilings.SUPPORTED["sqlalchemy"][1] == (2, 1)
    assert ceilings.violations() == []
