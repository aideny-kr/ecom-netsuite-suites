"""Guard: the Alembic revision tree must always have exactly one head.

Multiple heads make `alembic upgrade head` fail at deploy time ("Multiple head
revisions are present"). This offline test (it parses the revision graph, never
touches a database) fails fast in CI the moment a branch introduces a parallel
head without reconciling it via a merge migration.

Added during the metric-catalog feat->main promotion, where feat's
``080_metric_definitions`` collided with main's ``080_learned_rules_rls`` (both
branching off ``079_order_ref_pattern``). Resolved by re-parenting the metric line onto
``080_learned_rules_rls`` (one linear history) — NOT a merge migration, because a merge
head makes ``alembic downgrade -1`` (the deploy migration-safety reversibility test) fail
with "Ambiguous walk".
"""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

_BACKEND_DIR = Path(__file__).resolve().parent.parent


def _script_directory() -> ScriptDirectory:
    config = Config(str(_BACKEND_DIR / "alembic.ini"))
    # Pin script_location to an absolute path so the test is independent of the
    # process working directory (alembic resolves a relative location off cwd).
    config.set_main_option("script_location", str(_BACKEND_DIR / "alembic"))
    return ScriptDirectory.from_config(config)


def test_single_alembic_head() -> None:
    heads = _script_directory().get_heads()
    assert len(heads) == 1, (
        f"Expected exactly one Alembic head, found {len(heads)}: {sorted(heads)}. "
        "Reconcile parallel branches — prefer re-parenting one lineage's base revision onto "
        "the other (keeps `alembic downgrade -1` working); a merge migration also unifies the "
        "heads but breaks the deploy reversibility test with 'Ambiguous walk'."
    )
