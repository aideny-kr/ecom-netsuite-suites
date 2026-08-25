"""CI invariant: the Celigo write guard's escape hatch stays contained.

The guard makes a class of bug unrepresentable only for as long as opting out
stays rare and visible. ``celigo_writes_allowed`` is deliberately a named,
greppable context manager rather than a flag precisely so that disabling the
guard is an act a reviewer can see -- but "a reviewer can see it" is only true
if someone is looking. This is the someone.

Two properties, both executed against the tree rather than asserted about it:

  1. In PRODUCTION code, the allow token appears only where the trusted Celigo
     flow lives. A new production call site must edit this allowlist, which is
     the review-visible act the whole design depends on.
  2. No textual SQL UPDATE/DELETE targets ``connections`` or ``mcp_connectors``.
     ``do_orm_execute`` sees ORM constructs, not ``text("UPDATE ...")``, so that
     is the one hole the guard cannot close at runtime. Zero exist today; this
     keeps it that way.
"""

import pathlib
import re

BACKEND = pathlib.Path(__file__).resolve().parents[1]

TOKEN = "celigo_writes_allowed"

# Production modules permitted to hold the Celigo write window.
#
# celigo_write_guard.py DEFINES it. connector_status.py is the dedicated Celigo
# flow -- the connect/reconnect and disconnect endpoints that verify the token
# and keep the REST connection in step with its paired celigo_mcp connector.
# Nothing else in app/ may write a Celigo row at all.
PRODUCTION_ALLOWLIST = {
    "app/services/celigo_write_guard.py",
    "app/api/v1/connector_status.py",
}

# Test modules that must hold the window to exercise a guarded writer.
# Kept explicit (not a glob) so adding one is a deliberate, reviewed edit.
TEST_ALLOWLIST = {
    "tests/api/test_celigo_write_guard.py",
    "tests/services/test_mcp_connector_service_celigo.py",
    "tests/test_connections.py",  # seeds Celigo rows to test the generic-DELETE refusal
    "tests/test_celigo_write_guard_containment.py",  # names the token to check for it
}

GUARDED_TABLES = ("connections", "mcp_connectors")

# These two files DOCUMENT the textual-DML rule (both quote
# `text("UPDATE mcp_connectors ...")` as the example of what is forbidden), so
# scanning them for it matches prose, not code.
#
# KNOWN NARROWING, stated rather than hidden: this is a textual scan, so real
# textual DML added to the guard module itself would not be caught here. That
# module is the guard; it imports no `text` and any DML in it would be reviewed
# as part of changing the guard.
SELF_DOCUMENTING = {
    "app/services/celigo_write_guard.py",
    "tests/test_celigo_write_guard_containment.py",
}


def _python_files():
    for root in ("app", "tests", "scripts"):
        for path in (BACKEND / root).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            yield path, path.relative_to(BACKEND).as_posix()


def test_allow_token_appears_only_in_allowlisted_modules():
    offenders = sorted(
        rel
        for path, rel in _python_files()
        if TOKEN in path.read_text() and rel not in PRODUCTION_ALLOWLIST | TEST_ALLOWLIST
    )
    assert not offenders, (
        f"{TOKEN} disables the Celigo write guard and must not spread. Found it in: {offenders}. "
        "If a new module genuinely needs to write a Celigo row, that is a design decision — "
        "add it to the allowlist in this file so the change is reviewed, do not silently opt out."
    )


def test_no_production_module_outside_the_celigo_flow_holds_the_window():
    """The sharper half of the check: a test opting out is visible and harmless;
    a production endpoint opting out is the exact failure this guard exists to
    prevent."""
    offenders = sorted(
        rel
        for path, rel in _python_files()
        if rel.startswith("app/") and TOKEN in path.read_text() and rel not in PRODUCTION_ALLOWLIST
    )
    assert not offenders, f"production modules holding the Celigo write window: {offenders}"


def test_no_textual_sql_dml_targets_the_guarded_tables():
    """``do_orm_execute`` catches ORM constructs, not textual DML -- this is the
    documented hole in the guard's coverage, and it is empty today."""
    pattern = re.compile(
        r"(?is)\b(update|delete\s+from)\s+(" + "|".join(GUARDED_TABLES) + r")\b",
    )
    offenders = []
    for path, rel in _python_files():
        if rel in SELF_DOCUMENTING:
            continue
        for match in pattern.finditer(path.read_text()):
            offenders.append(f"{rel}: {match.group(0)!r}")
    assert not offenders, (
        "textual SQL DML against a guarded table bypasses the session-flush guard entirely "
        f"(do_orm_execute only sees ORM constructs). Found: {offenders}"
    )


class TestOperatorScriptRefusal:
    """Non-coverage item 1: ``scripts/import_tenant.py`` writes below the ORM
    with generic textual SQL, so the flush guard cannot see it. Instead of
    handing a bulk-import tool a blanket opt-out, the script carries its own
    refusal.
    """

    def test_celigo_rows_are_dropped_by_default(self):
        from scripts.import_tenant import _drop_celigo_rows

        rows = [
            {"id": "1", "provider": "celigo"},
            {"id": "2", "provider": "stripe"},
            {"id": "3", "provider": "celigo_mcp"},
            {"id": "4", "provider": "netsuite"},
        ]
        kept, dropped = _drop_celigo_rows("connections", rows)

        assert dropped == 2
        assert [r["provider"] for r in kept] == ["stripe", "netsuite"]

    def test_rows_without_a_provider_column_are_untouched(self):
        """Most tables in IMPORT_ORDER have no `provider` at all -- the filter
        must not silently drop, say, every tenant_config row."""
        from scripts.import_tenant import _drop_celigo_rows

        rows = [{"id": "1", "name": "Acme"}, {"id": "2", "name": "Globex"}]
        kept, dropped = _drop_celigo_rows("tenants", rows)

        assert dropped == 0
        assert kept == rows

    def test_the_opt_in_flag_exists_and_is_off_by_default(self):
        import inspect

        from scripts import import_tenant

        sig = inspect.signature(import_tenant.import_tenant)
        assert sig.parameters["allow_celigo"].default is False
        assert "--allow-celigo" in inspect.getsource(import_tenant.main)


def test_the_guard_is_registered_from_both_models():
    """Restates the anti-drift wiring as a containment rule: if either import is
    dropped, an entrypoint that touches only the other model still installs the
    listener -- but the one that touches neither would not, so both must stay."""
    for module in ("connection.py", "mcp_connector.py"):
        src = (BACKEND / "app" / "models" / module).read_text()
        assert "app.services.celigo_write_guard" in src, (
            f"app/models/{module} must import the guard: registering from an app entrypoint "
            "instead would leave workers, scripts, and the test harness unguarded."
        )
