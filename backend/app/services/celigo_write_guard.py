"""Session-flush choke point for Celigo's paired rows.

WHY THIS EXISTS (read before changing anything here).

Celigo is two rows that only make sense together: a ``connections`` row with
``provider='celigo'`` (the REST token) and an ``mcp_connectors`` row with
``provider='celigo_mcp'`` (the chat agent's read-only tools). Mutating one
without the other produces states that are individually valid and jointly
wrong -- the UI reports "connected" while the agent's tools are dead, or a
revoked connection is reactivated with a token nobody re-verified.

Three consecutive review rounds each found a *different* generic,
provider-agnostic endpoint doing exactly that, and each was fixed at its own
call site. A census then enumerated 23 mutation paths and established that no
service-layer choke point exists: 4 of 6 by-ID ``Connection`` mutators write
ORM attributes directly in the router, ``delete_connection`` uses its own
inline ``select`` rather than ``get_connection``, and no DB trigger exists
anywhere in the migration history. Per-call-site fixes cannot close the class.

The only point EVERY mutation passes is the SQLAlchemy session flush. So the
guard lives there: a ``before_flush`` listener on ``Session`` refuses any
create/update/delete of a Celigo row unless the write happens inside
``celigo_writes_allowed(session)``. Whatever a new endpoint does next month --
service call, direct attribute write, ``db.add``, ``db.delete`` -- it must
flush, and the flush refuses. Opting out is a grep-able, review-visible act of
wrapping the write in a named context manager, not a silent omission.

Registered on the ``Session`` CLASS, not on a sessionmaker, because this
codebase builds sessions four different ways and only one of them is a
factory:
  * ``app.core.database.async_session_factory``  (API requests)
  * ``app.core.database.worker_async_session()`` (per-Celery-task async engine)
  * ``sqlalchemy.orm.Session(sync_engine)``      (sync worker tasks -- no factory)
  * ``AsyncSession(bind=conn, ...)``             (the pytest harness -- no factory)
``AsyncSession`` is not a ``Session`` subclass; it wraps one, so a class-level
listener on ``Session`` covers the async sessions through their ``sync_session``
and shares the same ``.info`` dict.

Registration is triggered from ``app.models.connection`` and
``app.models.mcp_connector`` (both import this module) so a session for these
models cannot be constructed without loading the registration. That import
direction is deliberately backwards -- it is the mitigation for registration
drift, and the reason this module imports nothing from ``app.models``.

WHAT THIS DOES NOT COVER -- do not read it as broader than it is:
  1. Operator scripts writing below the ORM via raw SQL (see
     ``scripts/import_tenant.py``, which carries its own refusal).
  2. Already-incoherent state. This prevents future incoherence; it repairs
     nothing.
  3. External truth. A token revoked on Celigo's side leaves both rows
     ``active`` -- that is the health-check domain, not this one.
  4. Deliberate misuse of the allow token (mitigated by the containment test
     in ``tests/test_celigo_write_guard_containment.py``).
  5. Textual SQL -- ``session.execute(text("UPDATE mcp_connectors ..."))``.
     ``do_orm_execute`` sees ORM constructs, not textual DML. Zero exist
     today; the containment test also fails on any that appear.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from sqlalchemy import event
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select as sa_select
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# What is guarded
# ---------------------------------------------------------------------------

# tablename -> the provider value that makes a row on that table a Celigo row.
# Matching on ``__tablename__`` rather than importing the mapped classes keeps
# this module free of any ``app.models`` import, which is what lets the models
# import IT (see module docstring).
GUARDED_TABLES: dict[str, str] = {
    "connections": "celigo",
    "mcp_connectors": "celigo_mcp",
}

_ALLOW_KEY = "celigo_writes_allowed"

_REFUSAL = (
    "Celigo is managed in Settings -> Integrations. A Celigo connection is a pair "
    "(the REST connection plus its celigo_mcp agent connector) that must change "
    "together, so provider-agnostic paths cannot mutate it: use POST "
    "/api/v1/connector-status/celigo to connect or reconnect and DELETE "
    "/api/v1/connector-status/celigo to disconnect."
)


class CeligoManagedElsewhereError(Exception):
    """A generic, provider-agnostic path tried to mutate a Celigo row.

    Defined here rather than in ``connection_service`` (its original home,
    which re-exports it for backward compatibility) because this module must
    not import anything from ``app.services.connection_service`` -- that would
    close an import cycle through ``app.models.connection``.
    """


class CeligoInvariantError(Exception):
    """A write INSIDE the allowed window would leave a Celigo row incoherent.

    Distinct from :class:`CeligoManagedElsewhereError`: that one means "wrong
    door", this one means the trusted flow itself is producing a bad row. It
    is a programming error, not operator error, so it is deliberately NOT
    mapped to a 4xx -- it should surface as a 500 and a stack trace.
    """


# ---------------------------------------------------------------------------
# The allow token
# ---------------------------------------------------------------------------


@contextmanager
def celigo_writes_allowed(session: Any) -> Iterator[Any]:
    """Permit Celigo-row writes on *session* for the duration of the block.

    Accepts either an ``AsyncSession`` or a sync ``Session`` -- ``.info`` is
    the same dict either way (``AsyncSession.info`` proxies its
    ``sync_session``), which is why the listener can read a flag the caller set
    on the async object.

    Re-entrant: ``connect_celigo`` wraps a block that itself calls
    ``_upsert_celigo_mcp_connector``, which opens its own window. The previous
    value is restored on exit rather than unconditionally cleared, so the inner
    block exiting does not silently disarm the outer one.

    Used as a plain ``with`` inside ``async def`` -- the block may contain
    ``await``; the flag is per-session state, and each request owns its
    session.
    """
    info = session.info
    previous = info.get(_ALLOW_KEY, False)
    info[_ALLOW_KEY] = True
    try:
        yield session
    finally:
        if previous:
            info[_ALLOW_KEY] = True
        else:
            info.pop(_ALLOW_KEY, None)


def writes_are_allowed(session: Any) -> bool:
    """Whether *session* currently carries the allow token."""
    return bool(session.info.get(_ALLOW_KEY))


# ---------------------------------------------------------------------------
# Row classification -- must never trigger I/O on the hot path
# ---------------------------------------------------------------------------

_UNSET = object()


def _attr(obj: Any, name: str, default_if_unset: Any = None) -> Any:
    """Read *name* off *obj* without gratuitously loading expired attributes.

    ``before_flush`` runs on every flush in the application, so the common case
    must be a dict lookup. ``state.dict`` holds exactly the currently-loaded
    values; a hit there is free.

    On a miss the object is either pending/transient (the attribute was never
    set, so the mapped column default is what will land -- *default_if_unset*)
    or persistent-and-expired, in which case falling back to ``getattr`` emits
    the SELECT that unexpires it. That fallback is safe here even under
    ``AsyncSession``: ``before_flush`` runs inside the greenlet that
    ``await session.flush()`` spawned, so synchronous lazy loads are legal --
    unlike the same access from ordinary async code, which raises
    ``MissingGreenlet``.
    """
    state = sa_inspect(obj)
    value = state.dict.get(name, _UNSET)
    if value is not _UNSET:
        return value
    if state.pending or state.transient:
        return default_if_unset
    return getattr(obj, name, default_if_unset)


def _provider_values(obj: Any) -> tuple[Any, ...]:
    """Every ``provider`` value this flush puts *obj* through -- new AND prior.

    Classifying on the CURRENT value alone is a hole, not a shortcut. An UPDATE
    that rewrites ``provider`` has already applied the new value by the time
    ``before_flush`` runs, so reading it makes a ``celigo`` -> ``netsuite``
    rename look like an ordinary NetSuite write: the row is laundered out of the
    guard in the same flush that mutates it. The inverse matters too -- a row
    renamed INTO ``celigo``/``celigo_mcp`` is a Celigo row from that write
    onward and must be guarded from it, not after it.

    So both ends are returned and the caller guards on either matching.

    ``AttributeState.history`` deliberately does NOT emit loader callables, so
    it is free, and for a row whose ``provider`` is loaded (the overwhelming
    case -- ``expire_on_commit=False`` on every session factory here) it is also
    complete. Only when it comes back blank on both sides does this fall through
    to ``_committed_provider_from_db``.
    """
    current = _attr(obj, "provider")
    state = sa_inspect(obj)
    if state.pending or state.transient:
        # No prior database value exists; ``current`` is what will land.
        return (current,)

    history = state.attrs["provider"].history
    previous = tuple(history.deleted) + tuple(history.unchanged)
    if not previous:
        stored = _committed_provider_from_db(state)
        if stored is not _UNSET:
            previous = (stored,)

    return (current, *previous)


def _committed_provider_from_db(state: Any) -> Any:
    """The row's STORED ``provider``, for when attribute history cannot say.

    Reached only when all three hold: the object is on a guarded table, its
    ``provider`` was changed in this flush, and the pre-change value was never
    loaded. ``expire()`` drops it, and SQLAlchemy then records ``NO_VALUE`` as
    the pre-change value on the SET rather than fetching -- so the history has
    an ``added`` and nothing else. ``load_history()`` cannot recover it either:
    the NEW value is sitting in ``__dict__``, so the loader callable it would
    fire is never reached.

    Failing closed on that blank instead would refuse an ordinary
    ``stripe`` -> ``shopify`` rename, and every non-Celigo write must keep
    behaving exactly as it always has. So this asks the database, which is
    exact. The cost is bounded to a case that requires an expired row AND a
    provider rename AND a guarded table; ordinary flushes never reach it.

    Safe inside ``before_flush``: ``Session._flushing`` is already True when the
    event fires, so ``Session._autoflush`` is suppressed and this cannot
    re-enter the flush; and the surrounding greenlet makes the I/O legal under
    ``AsyncSession`` for the same reason ``_attr``'s ``getattr`` fallback is.
    """
    session = state.session
    identity = state.identity
    if session is None or identity is None:
        return _UNSET

    mapper = state.mapper
    stmt = sa_select(mapper.columns["provider"]).where(
        *[column == value for column, value in zip(mapper.primary_key, identity)]
    )
    return session.execute(stmt).scalar_one_or_none()


def _guarded_table(obj: Any) -> str | None:
    """Return the guarded tablename *obj* belongs to, or None.

    Cheapest possible reject FIRST: one class-attribute lookup and one dict
    lookup rejects every non-guarded table, which is every object in almost
    every flush in the application. Nothing below runs for those.
    """
    tablename = getattr(type(obj), "__tablename__", None)
    if tablename not in GUARDED_TABLES:
        return None
    if GUARDED_TABLES[tablename] not in _provider_values(obj):
        return None
    return tablename


# ---------------------------------------------------------------------------
# Field invariants -- enforced even inside the allowed window
# ---------------------------------------------------------------------------


def _allowed_mcp_server_urls() -> frozenset[str]:
    """Celigo's pinned hosted MCP URLs, one per region.

    Imported lazily so this module keeps zero ``app`` imports at module scope.
    It is imported by ``app.models``, and a model importing a service that
    imports another service is exactly how an import cycle gets built by
    accident later.
    """
    from app.services.celigo.client import CELIGO_MCP_SERVER_URLS

    return frozenset(CELIGO_MCP_SERVER_URLS.values())


def _enforce_invariants(obj: Any, tablename: str) -> None:
    """The pairwise field rules ``_upsert_celigo_mcp_connector`` maintains by hand.

    Hand-maintained invariants decay: an earlier version of that function set
    ``status`` and ``is_enabled`` in separate branches and could land a failed
    reconnect on ``status='active'`` + ``is_enabled=False``. Checking them at
    the flush makes the incoherent pair unrepresentable rather than merely
    currently-absent.
    """
    if tablename != "mcp_connectors":
        return

    server_url = _attr(obj, "server_url")
    allowed = _allowed_mcp_server_urls()
    if server_url not in allowed:
        # A celigo_mcp row's server_url decides which host the agent's tools
        # are discovered from, and celigo_tool_policy trusts the tool NAMES
        # that host reports. An attacker-chosen URL turns the read-only
        # boundary into whatever that server says it is.
        raise CeligoInvariantError(
            f"celigo_mcp server_url must be one of Celigo's pinned hosted MCP URLs "
            f"({sorted(allowed)}), got {server_url!r}"
        )

    # `is_enabled` defaults to True at the column, so an unset value on a
    # pending row still becomes True at INSERT -- check the value that will
    # actually land, not the one currently in __dict__.
    is_enabled = _attr(obj, "is_enabled", default_if_unset=True)
    status = _attr(obj, "status", default_if_unset="active")
    if is_enabled is True and status != "active":
        raise CeligoInvariantError(
            f"celigo_mcp row cannot be is_enabled=True with status={status!r}; "
            "agent access must reflect a live connector"
        )


# ---------------------------------------------------------------------------
# Listeners
# ---------------------------------------------------------------------------


def _refuse_or_verify(obj: Any, tablename: str, allowed: bool, verb: str) -> None:
    if not allowed:
        raise CeligoManagedElsewhereError(_REFUSAL)
    if verb != "delete":
        _enforce_invariants(obj, tablename)


def _before_flush(session: Session, flush_context: Any, instances: Any) -> None:
    """Refuse Celigo-row writes that did not come through the dedicated flow."""
    allowed = writes_are_allowed(session)

    for obj in session.new:
        tablename = _guarded_table(obj)
        if tablename is not None:
            _refuse_or_verify(obj, tablename, allowed, "create")

    for obj in session.dirty:
        tablename = _guarded_table(obj)
        # session.dirty is populated by any attribute SET, including one that
        # wrote back the same value. is_modified() is the real net-change
        # test, so a no-op touch is not treated as a mutation.
        if tablename is not None and session.is_modified(obj):
            _refuse_or_verify(obj, tablename, allowed, "update")

    for obj in session.deleted:
        tablename = _guarded_table(obj)
        if tablename is not None:
            _refuse_or_verify(obj, tablename, allowed, "delete")


def _do_orm_execute(orm_execute_state: Any) -> None:
    """Tripwire for ORM-enabled bulk ``update()``/``delete()``.

    Bulk DML bypasses the unit of work entirely -- no objects, no flush, no
    ``before_flush``. ZERO such statements exist against these two tables
    today; this exists so the first one written is refused rather than
    silently punching through the guard. It is deliberately table-wide (not
    provider-aware): a bulk statement's WHERE clause cannot be evaluated
    without running it, so "does this hit a Celigo row?" is unanswerable here.
    """
    if not orm_execute_state.is_orm_statement:
        return
    if not (orm_execute_state.is_update or orm_execute_state.is_delete):
        return
    if writes_are_allowed(orm_execute_state.session):
        return

    table = getattr(orm_execute_state.statement, "table", None)
    tablename = getattr(table, "name", None)
    if tablename in GUARDED_TABLES:
        raise CeligoManagedElsewhereError(
            f"{_REFUSAL} (refused a bulk ORM UPDATE/DELETE against {tablename!r}, "
            "which would bypass the per-row guard entirely)"
        )


_REGISTERED = False


def register_listeners() -> None:
    """Install the guard on the ``Session`` class. Idempotent.

    Called at import time below. Exposed as a function so a test can assert
    calling it twice does not double-register.
    """
    global _REGISTERED
    if _REGISTERED:
        return
    event.listen(Session, "before_flush", _before_flush)
    event.listen(Session, "do_orm_execute", _do_orm_execute)
    _REGISTERED = True


register_listeners()
