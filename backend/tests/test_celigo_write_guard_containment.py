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
  2. No textual SQL UPDATE/DELETE targets ``connections`` or ``mcp_connectors``,
     outside ``DML_TEST_ALLOWLIST`` -- a deliberately narrow, explicit set of
     test files that exercise the FK's ON DELETE SET NULL behaviour, which
     only raw SQL can trigger (celigo_write_guard refuses any ORM delete of a
     celigo row). ``do_orm_execute`` sees ORM constructs, not
     ``text("UPDATE ...")``, so that is the one hole the guard cannot close at
     runtime; this keeps everywhere else clean.
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
    "tests/services/test_celigo_dispatcher_feature_flag.py",  # seeds a celigo_mcp row to drive the dispatcher
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

# FIX ROUND 5 (authorised by team lead 2026-08-27): unlike SELF_DOCUMENTING
# above, these two files don't merely QUOTE the DML pattern as prose -- they
# genuinely EXECUTE it, deliberately. Both seed a `provider='celigo'`
# connection and must delete it with raw SQL, because celigo_write_guard
# refuses any ORM flush/delete of a celigo row outside the paired
# connect/disconnect flow -- raw SQL is the only way to exercise the FK's
# ON DELETE SET NULL behaviour on celigo_flow_errors/celigo_error_signatures.
# Kept as its own set (not folded into SELF_DOCUMENTING) so that set's own
# name stays honest -- everything in it truly is prose, nothing here is.
#
# FIX ROUND 6 (whole-branch review finding 13, 2026-08-27): this USED TO
# exempt these two files ENTIRELY from the scan below, so a future raw
# `UPDATE connections SET ...` anywhere in either file would have been
# invisible to the guard -- not just the one delete-by-id statement they
# actually need. Every DML match in both files today (confirmed by grep) is
# exactly `DELETE FROM connections WHERE id = :<param>` -- a single-row
# delete by primary key, nothing wider. `_ALLOWED_DELETE_BY_ID` below is that
# NARROW shape; only text matching it is stripped out of these two files
# before the scan runs, so anything else DML in them -- an UPDATE, a bare
# DELETE, a DELETE on mcp_connectors -- still trips the guard exactly like it
# would anywhere else. FIX ROUND 9 (re-review R6): the shape must also END
# there -- a statement that merely STARTS with it and then widens
# (`... WHERE id = :id OR tenant_id = :t`) is caught, not exempted.
DML_TEST_ALLOWLIST = {
    "tests/test_celigo_flow_map_rls.py",
    "tests/test_celigo_repository.py",
}

# FIX ROUND 9 (scoped re-review R6, 2026-08-27): this used to end at `\b`
# after the bind parameter, so it matched a PREFIX -- `DELETE FROM connections
# WHERE id = :id OR tenant_id = :t` had its authorised head stripped and its
# widening tail left behind as text the DML pattern below no longer
# recognises, which is the whole-file exemption's hole reintroduced one
# statement at a time. The trailing lookahead requires the match to run to the
# END of the SQL string literal, so the exemption covers the entire statement
# or none of it. (Lookahead, not a consumed character, so the quote stays in
# the text and cannot silently join two adjacent literals together.)
_ALLOWED_DELETE_BY_ID = re.compile(r"""(?is)\bdelete\s+from\s+connections\s+where\s+id\s*=\s*:\w+\s*(?=["'])""")


def _python_files(root: pathlib.Path = BACKEND):
    for subdir in ("app", "tests", "scripts"):
        for path in (root / subdir).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            yield path, path.relative_to(root).as_posix()


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


def _find_dml_offenders(root: pathlib.Path = BACKEND) -> list[str]:
    """Scan every ``.py`` file under *root* for textual SQL DML against
    ``GUARDED_TABLES``. ``SELF_DOCUMENTING`` files are skipped entirely
    (prose, not code). ``DML_TEST_ALLOWLIST`` files are NOT skipped -- only
    the specific, deliberately-authorised ``_ALLOWED_DELETE_BY_ID`` shape is
    stripped out of them before the pattern runs, so anything else DML in
    those two files is still caught (FIX ROUND 6, finding 13). Factored out
    of the test below so a synthetic tree can exercise the statement-level
    scoping without seeding a real forbidden statement into a real
    allowlisted file, which would itself be the bug this guards against."""
    pattern = re.compile(
        r"(?is)\b(update|delete\s+from)\s+(" + "|".join(GUARDED_TABLES) + r")\b",
    )
    offenders = []
    for path, rel in _python_files(root):
        if rel in SELF_DOCUMENTING:
            continue
        content = path.read_text()
        if rel in DML_TEST_ALLOWLIST:
            content = _ALLOWED_DELETE_BY_ID.sub("", content)
        for match in pattern.finditer(content):
            offenders.append(f"{rel}: {match.group(0)!r}")
    return offenders


def test_no_textual_sql_dml_targets_the_guarded_tables():
    """``do_orm_execute`` catches ORM constructs, not textual DML -- this is the
    documented hole in the guard's coverage, and it is empty today."""
    offenders = _find_dml_offenders()
    assert not offenders, (
        "textual SQL DML against a guarded table bypasses the session-flush guard entirely "
        f"(do_orm_execute only sees ORM constructs). Found: {offenders}"
    )


class TestDmlAllowlistIsStatementScopedNotFileScoped:
    """FIX ROUND 6 (whole-branch review finding 13, 2026-08-27): proves the
    narrowing above against a SYNTHETIC tree, not the real allowlisted
    files -- seeding a genuine `UPDATE connections ...` into either real file
    to prove the guard catches it would itself be the exact bug under test."""

    def test_an_update_alongside_the_allowed_delete_is_still_caught(self, tmp_path):
        allowlisted_rel = next(iter(DML_TEST_ALLOWLIST))
        target = tmp_path / allowlisted_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            'await db.execute(text("DELETE FROM connections WHERE id = :id").bindparams(id=conn_id))\n'
            "# a hypothetical FUTURE statement this file has no business adding:\n"
            "await db.execute(text(\"UPDATE connections SET status = 'error'\"))\n"
        )

        offenders = _find_dml_offenders(tmp_path)

        assert any("UPDATE connections" in o for o in offenders), (
            "a whole-file exemption would hide this -- the allowlist must be statement-scoped"
        )
        assert not any("DELETE FROM connections" in o for o in offenders), (
            "the one deliberately-authorised delete-by-id statement must still be exempt"
        )

    def test_a_delete_that_only_starts_with_the_allowed_shape_is_still_caught(self, tmp_path):
        """SCOPED RE-REVIEW R6 (2026-08-27, PROVEN against a synthetic tree):
        the narrowed pattern matched a PREFIX, so a WIDENED delete that merely
        begins with the authorised shape -- `... WHERE id = :id OR tenant_id
        = :t` -- had its authorised head stripped and its dangerous tail left
        behind as text the DML pattern no longer recognises. The allowlist
        must exempt the whole statement or nothing: the match now has to run
        to the end of the SQL string literal."""
        allowlisted_rel = next(iter(DML_TEST_ALLOWLIST))
        target = tmp_path / allowlisted_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            'await db.execute(text("DELETE FROM connections WHERE id = :id").bindparams(id=conn_id))\n'
            "# the same shape, widened -- no longer a single-row delete by primary key:\n"
            'await db.execute(text("DELETE FROM connections WHERE id = :id OR tenant_id = :t"))\n'
        )

        offenders = _find_dml_offenders(tmp_path)

        assert any("DELETE FROM connections" in o for o in offenders), (
            "a delete that widens past the authorised single-row shape must not inherit its exemption"
        )
        # ...and the authorised statement in the same file is still exempt, so
        # the fix is a tightening, not a blanket revocation.
        assert len(offenders) == 1


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
