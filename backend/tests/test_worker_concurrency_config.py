"""The worker count must not drift between the image and the rate limiter.

Rate limits in this codebase are expressed FLEET-WIDE. When Redis is unavailable the
limiter degrades to a per-process counter and divides the fleet-wide number by
`settings.WEB_CONCURRENCY` (core/rate_limit.py `_per_process_limit`), so that value
has to equal the number of uvicorn workers the image actually starts.

Gate round 5, major: it did not. `WEB_CONCURRENCY` defaulted to 4 with a comment
citing `backend/Dockerfile` (`--workers 4`), but production is built from
`backend/Dockerfile.prod`, which runs `--workers 2`, and nothing set the env var in
either image. Every fallback ceiling in production was therefore computed against a
worker count that does not exist -- the same class of bug (a limit silently scaled by
the wrong replica count) that this whole change set out to remove from the MCP path.

These tests read the Dockerfiles rather than restating their numbers, so the
invariant survives someone editing `--workers`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
IMAGES = [BACKEND / "Dockerfile", BACKEND / "Dockerfile.prod"]


def _workers_in_cmd(text: str) -> int | None:
    """The N in `uvicorn ... --workers N` from the image's CMD."""
    m = re.search(r'--workers"?,?\s*"?(\d+)', text)
    return int(m.group(1)) if m else None


def _env_web_concurrency(text: str) -> int | None:
    """The N in `ENV WEB_CONCURRENCY=N`, including inside a multi-line ENV block."""
    m = re.search(r"WEB_CONCURRENCY=(\d+)", text)
    return int(m.group(1)) if m else None


@pytest.mark.parametrize("dockerfile", IMAGES, ids=lambda p: p.name)
def test_image_declares_web_concurrency_matching_its_worker_count(dockerfile: Path):
    text = dockerfile.read_text()

    workers = _workers_in_cmd(text)
    assert workers is not None, f"{dockerfile.name} has no `--workers N` to check against"

    declared = _env_web_concurrency(text)
    assert declared is not None, (
        f"{dockerfile.name} starts {workers} uvicorn workers but never sets "
        "ENV WEB_CONCURRENCY, so the limiter falls back to the pydantic default and "
        "divides fleet-wide ceilings by the wrong number"
    )
    assert declared == workers, (
        f"{dockerfile.name}: ENV WEB_CONCURRENCY={declared} but the image starts "
        f"{workers} workers — the in-memory fallback would mis-scale every limit"
    )


def test_default_matches_the_production_image():
    """An unset env must not silently assume the dev image's worker count.

    The default only applies outside the containers (local runs, tests), but if it is
    going to be wrong somewhere it must be wrong in the direction that does not
    inflate a production ceiling.
    """
    from app.core.config import Settings

    prod_workers = _workers_in_cmd((BACKEND / "Dockerfile.prod").read_text())
    assert Settings.model_fields["WEB_CONCURRENCY"].default == prod_workers, (
        f"WEB_CONCURRENCY's default should track the production image (--workers {prod_workers}), not the dev one"
    )


def test_mcp_rebaseline_factor_matches_the_production_image():
    """Gate round 6, major. The MCP re-baseline was scaled off the wrong image.

    On 2026-08-06 every TOOL_CONFIGS rate_limit_per_minute was multiplied to convert
    a per-process ceiling into a fleet-wide one, on the stated basis that "uvicorn
    runs --workers 4 (backend/Dockerfile)". Production is not built from that file --
    it is built from Dockerfile.prod, which runs 2. So the shared-Redis ceilings
    shipped ~2x looser than anything production ever actually enforced.

    That is the very defect this work exists to end: the number in TOOL_CONFIGS not
    being the number enforced. Pinned to the image so it cannot drift again.
    """
    from app.mcp.governance import MCP_REBASELINE_FACTOR

    prod_workers = _workers_in_cmd((BACKEND / "Dockerfile.prod").read_text())
    assert MCP_REBASELINE_FACTOR == prod_workers, (
        f"TOOL_CONFIGS were scaled by {MCP_REBASELINE_FACTOR} but production runs "
        f"{prod_workers} workers — the enforced ceiling is not the historical one"
    )
