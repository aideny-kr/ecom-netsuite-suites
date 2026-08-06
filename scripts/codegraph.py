#!/usr/bin/env python3
"""codegraph — a deterministic code index. No LLM, no embeddings, no network.

Answers the structural questions that currently cost a grep and a lot of sifting:
where is X defined, who calls it, who imports this module, what routes exist, and
what breaks if I change this. Every edge is derived from the Python AST, so the
answers are reproducible and cannot be hallucinated — the same property that makes
a code graph worth building before any LLM-extracted graph.

Measured baseline on this repo before it existed (2026-08-06):

    grep release_lock        ->  28 hits,   1 is the definition
    grep set_tenant_context  -> 202 hits,   3 are definitions
    grep InstrumentedTask    ->  75 hits,   1 is the definition

The cost is not the grep, it is reading 202 lines to find 3. That ratio is the
thing being optimised, and the kill rule was set before the build: if this does not
cut sift volume by 5x, delete it.

    scripts/codegraph.py index [--full]
    scripts/codegraph.py def <name>
    scripts/codegraph.py callers <name>
    scripts/codegraph.py importers <module-substring>
    scripts/codegraph.py routes [pattern]
    scripts/codegraph.py impact <name>       # def + callers + callers-of-callers
    scripts/codegraph.py stats

Incremental by content hash: unchanged files are not re-parsed, so a reindex after
a branch switch costs roughly the size of the diff rather than the repo.

TypeScript is indexed by REGEX, not a parser. That is a deliberate 80/20 — exports
and imports are regular enough to match reliably, call edges are not. TS symbols are
marked `ts-approx` in output so a result is never mistaken for AST-grade truth.
Python is the AST-backed half and the one to trust.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import os
import re
import sqlite3
import subprocess
import sys
import time

DB_NAME = "codegraph.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS files   (path TEXT PRIMARY KEY, lang TEXT, hash TEXT);
CREATE TABLE IF NOT EXISTS symbols (name TEXT, kind TEXT, path TEXT, line INTEGER, parent TEXT);
CREATE TABLE IF NOT EXISTS refs    (name TEXT, path TEXT, line INTEGER, ctx TEXT, kind TEXT);
CREATE TABLE IF NOT EXISTS imports (path TEXT, module TEXT, name TEXT, line INTEGER);
CREATE TABLE IF NOT EXISTS routes  (method TEXT, route TEXT, handler TEXT, path TEXT, line INTEGER);
CREATE INDEX IF NOT EXISTS i_sym_name ON symbols(name);
CREATE INDEX IF NOT EXISTS i_ref_name ON refs(name, kind);
CREATE INDEX IF NOT EXISTS i_imp_mod  ON imports(module);
CREATE INDEX IF NOT EXISTS i_sym_path ON symbols(path);
CREATE INDEX IF NOT EXISTS i_ref_path ON refs(path);
"""

# @router.get("/path"), @app.post("/x"), @celery_app.task(name="tasks.y")
_ROUTE_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def repo_root() -> str:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
    )
    if out.returncode:
        sys.exit("codegraph: not inside a git repository")
    return out.stdout.strip()


def db_path() -> str:
    """Per-worktree, inside the git dir.

    NOT `<root>/.git/...`: in a worktree `.git` is a FILE containing a gitdir
    pointer, so that path is unopenable — which is exactly how this failed on the
    first run. `--git-dir` resolves to the real per-worktree directory, which is
    also the correct scope: two worktrees hold different code and must not share
    an index. It sits inside the git dir so it is never committed and never needs
    a .gitignore entry.
    """
    out = subprocess.run(
        ["git", "rev-parse", "--absolute-git-dir"], capture_output=True, text=True
    )
    if out.returncode:
        sys.exit("codegraph: not inside a git repository")
    return os.path.join(out.stdout.strip(), DB_NAME)


def tracked_files(root: str) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "*.py", "*.ts", "*.tsx"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    return [f for f in out.stdout.splitlines() if f]


def _hash(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha1(fh.read()).hexdigest()
    except Exception:
        return ""


# --------------------------------------------------------------------------- python
class _PyVisitor(ast.NodeVisitor):
    """One pass, collecting definitions, call edges, imports and route decorators.

    Call edges resolve to the *attribute tail* (`a.b.call` -> `call`). Unqualified
    names collide across modules, which is exactly why `callers` output always shows
    the file: the index narrows the candidate set, it does not pretend to resolve
    every binding. Full resolution needs type inference, and its absence is a known
    limit rather than a silent inaccuracy.
    """

    def __init__(self, path: str):
        self.path = path
        self.symbols: list[tuple] = []
        self.refs: list[tuple] = []
        self.imports: list[tuple] = []
        self.routes: list[tuple] = []
        self._stack: list[str] = []
        self._skip: set[int] = set()

    def _parent(self) -> str:
        return self._stack[-1] if self._stack else ""

    def _decorators(self, node) -> None:
        for dec in getattr(node, "decorator_list", []):
            if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                continue
            method = dec.func.attr.lower()
            if method in _ROUTE_METHODS and dec.args:
                first = dec.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    self.routes.append(
                        (method.upper(), first.value, node.name, self.path, node.lineno)
                    )
            elif method == "task":
                name = next(
                    (
                        kw.value.value
                        for kw in dec.keywords
                        if kw.arg == "name" and isinstance(kw.value, ast.Constant)
                    ),
                    node.name,
                )
                self.routes.append(
                    ("TASK", str(name), node.name, self.path, node.lineno)
                )

    def _visit_def(self, node, kind: str) -> None:
        self.symbols.append((node.name, kind, self.path, node.lineno, self._parent()))
        self._decorators(node)
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()

    def visit_FunctionDef(self, node):
        self._visit_def(node, "func")

    def visit_AsyncFunctionDef(self, node):
        self._visit_def(node, "func")

    def visit_ClassDef(self, node):
        self.symbols.append(
            (node.name, "class", self.path, node.lineno, self._parent())
        )
        for base in node.bases:
            tail = (
                base.attr
                if isinstance(base, ast.Attribute)
                else getattr(base, "id", None)
            )
            if tail:
                # Inheritance is a real edge: "who subclasses InstrumentedTask" is a
                # question grep answers with every mention of the word.
                self.refs.append(
                    (tail, self.path, node.lineno, f"base-of:{node.name}", "base")
                )
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()

    def visit_Call(self, node):
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
        if name:
            self.refs.append(
                (name, self.path, node.lineno, self._parent() or "<module>", "call")
            )
        # The callee node is also a Name/Attribute load; skip it below so a call is
        # not double-counted as a bare use.
        self._skip.add(id(f))
        self.generic_visit(node)

    def visit_Name(self, node):
        # Bare value references. Without these, `base=InstrumentedTask` is invisible
        # and the index answers "0 callers" for a symbol wired into every worker —
        # a false negative that reads as authoritative, which is worse than no index.
        if isinstance(node.ctx, ast.Load) and id(node) not in self._skip:
            self.refs.append(
                (node.id, self.path, node.lineno, self._parent() or "<module>", "use")
            )
        self.generic_visit(node)

    def visit_Import(self, node):
        for a in node.names:
            self.imports.append((self.path, a.name, a.asname or a.name, node.lineno))

    def visit_ImportFrom(self, node):
        mod = node.module or ""
        for a in node.names:
            self.imports.append((self.path, mod, a.name, node.lineno))


def parse_python(root: str, rel: str):
    try:
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=rel)
    except (SyntaxError, UnicodeDecodeError, OSError):
        # A file that will not parse is reported by `stats`, never silently dropped:
        # a silently-missing file makes the index quietly wrong, which is worse than
        # not having one.
        return None
    v = _PyVisitor(rel)
    v.visit(tree)
    return v


# ------------------------------------------------------------------------- typescript
_TS_SYM = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?"
    r"(?:(?P<kind>function|class|interface|type|const|let)\s+)"
    r"(?P<name>[A-Za-z_$][\w$]*)",
    re.M,
)
_TS_IMP = re.compile(
    r"""^\s*import\s+(?:.*?\s+from\s+)?['"](?P<mod>[^'"]+)['"]""", re.M
)


def parse_ts(root: str, rel: str):
    try:
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            src = fh.read()
    except (UnicodeDecodeError, OSError):
        return None
    v = _PyVisitor(rel)
    for m in _TS_SYM.finditer(src):
        line = src.count("\n", 0, m.start()) + 1
        v.symbols.append((m.group("name"), "ts-approx", rel, line, ""))
    for m in _TS_IMP.finditer(src):
        line = src.count("\n", 0, m.start()) + 1
        v.imports.append((rel, m.group("mod"), "", line))
    return v


# ------------------------------------------------------------------------------ index
def cmd_index(args) -> int:
    root = repo_root()
    dbp = db_path()
    conn = sqlite3.connect(dbp)
    conn.executescript(SCHEMA)
    if args.full:
        for t in ("files", "symbols", "refs", "imports", "routes"):
            conn.execute(f"DELETE FROM {t}")

    known = dict(conn.execute("SELECT path, hash FROM files").fetchall())
    files = tracked_files(root)
    t0 = time.time()
    changed = skipped = failed = 0

    seen = set()
    for rel in files:
        seen.add(rel)
        h = _hash(os.path.join(root, rel))
        if not h:
            continue
        if known.get(rel) == h:
            skipped += 1
            continue
        changed += 1
        for t in ("symbols", "refs", "imports", "routes"):
            conn.execute(f"DELETE FROM {t} WHERE path = ?", (rel,))
        v = parse_python(root, rel) if rel.endswith(".py") else parse_ts(root, rel)
        if v is None:
            failed += 1
            conn.execute(
                "INSERT OR REPLACE INTO files VALUES (?,?,?)", (rel, "unparsed", h)
            )
            continue
        conn.executemany("INSERT INTO symbols VALUES (?,?,?,?,?)", v.symbols)
        conn.executemany("INSERT INTO refs    VALUES (?,?,?,?,?)", v.refs)
        conn.executemany("INSERT INTO imports VALUES (?,?,?,?)", v.imports)
        conn.executemany("INSERT INTO routes  VALUES (?,?,?,?,?)", v.routes)
        conn.execute(
            "INSERT OR REPLACE INTO files VALUES (?,?,?)",
            (rel, "py" if rel.endswith(".py") else "ts", h),
        )

    # Deleted files must lose their rows or the index answers with code that is gone —
    # a stale graph is confident, structured and wrong.
    gone = [p for p in known if p not in seen]
    for p in gone:
        for t in ("files", "symbols", "refs", "imports", "routes"):
            conn.execute(f"DELETE FROM {t} WHERE path = ?", (p,))

    conn.commit()
    print(
        f"indexed {changed} changed, {skipped} unchanged, {len(gone)} removed, "
        f"{failed} unparsed in {time.time() - t0:.1f}s -> {dbp}"
    )
    return 0


# ----------------------------------------------------------------------------- queries
def _conn() -> sqlite3.Connection:
    p = db_path()
    if not os.path.exists(p):
        sys.exit("codegraph: no index yet — run: scripts/codegraph.py index")
    return sqlite3.connect(p)


def cmd_def(args) -> int:
    rows = (
        _conn()
        .execute(
            "SELECT kind, path, line, parent FROM symbols WHERE name = ? ORDER BY kind, path",
            (args.name,),
        )
        .fetchall()
    )
    if not rows:
        print(f"no definition of {args.name!r}")
        return 1
    for kind, path, line, parent in rows:
        where = f" (in {parent})" if parent else ""
        print(f"{path}:{line}  {kind} {args.name}{where}")
    return 0


def cmd_callers(args) -> int:
    conn = _conn()
    defs = conn.execute(
        "SELECT path, line FROM symbols WHERE name = ?", (args.name,)
    ).fetchall()
    defpaths = {p for p, _ in defs}
    rows = conn.execute(
        "SELECT path, line, ctx FROM refs WHERE name = ? AND kind = 'call' ORDER BY path, line",
        (args.name,),
    ).fetchall()
    # A definition site is not a caller. Excluding it is most of the noise removed.
    rows = [
        r for r in rows if not (r[0] in defpaths and any(r[1] == dl for _, dl in defs))
    ]

    # A symbol can be used without ever being CALLED — `base=InstrumentedTask`,
    # a decorator argument, a value passed to a registry. Reporting a bare 0 there
    # reads as "nothing depends on this" and is how a structured answer becomes a
    # confidently wrong one. So zero callers always reports the other kinds too.
    other = conn.execute(
        "SELECT kind, COUNT(*) FROM refs WHERE name = ? AND kind != 'call' GROUP BY kind",
        (args.name,),
    ).fetchall()

    for path, line, ctx in rows:
        print(f"{path}:{line}  in {ctx}")
    print(f"-- {len(rows)} call site(s), {len(defs)} definition(s)")
    if other:
        summary = ", ".join(f"{n} {k}" for k, n in other)
        print(
            f"-- NOT calls, but references: {summary}  (see: codegraph.py uses {args.name})"
        )
    return 0 if rows or other else 1


def cmd_uses(args) -> int:
    """Every reference of every kind. Use this before deleting or renaming anything."""
    rows = (
        _conn()
        .execute(
            "SELECT kind, path, line, ctx FROM refs WHERE name = ? ORDER BY kind, path, line",
            (args.name,),
        )
        .fetchall()
    )
    if not rows:
        print(f"no references to {args.name!r}")
        return 1
    for kind, path, line, ctx in rows:
        print(f"{kind:5} {path}:{line}  in {ctx}")
    print(f"-- {len(rows)} reference(s)")
    return 0


def cmd_importers(args) -> int:
    rows = (
        _conn()
        .execute(
            "SELECT DISTINCT path, module, name FROM imports WHERE module LIKE ? OR name = ? "
            "ORDER BY path",
            (f"%{args.module}%", args.module),
        )
        .fetchall()
    )
    for path, module, name in rows:
        print(f"{path}  <- {module}{'.' + name if name else ''}")
    print(f"-- {len(rows)} importer(s)")
    return 0 if rows else 1


def cmd_routes(args) -> int:
    q = "SELECT method, route, handler, path, line FROM routes"
    p: tuple = ()
    if args.pattern:
        q += " WHERE route LIKE ? OR path LIKE ?"
        p = (f"%{args.pattern}%", f"%{args.pattern}%")
    rows = _conn().execute(q + " ORDER BY method, route", p).fetchall()
    for method, route, handler, path, line in rows:
        print(f"{method:6} {route:52} {handler}  ({path}:{line})")
    print(f"-- {len(rows)} route(s)/task(s)")
    return 0 if rows else 1


def cmd_impact(args) -> int:
    """Two hops. The first hop is what grep gives you; the second is the reason to
    have a graph at all — 'what else moves if this changes'."""
    conn = _conn()
    direct = conn.execute(
        "SELECT DISTINCT path, ctx FROM refs WHERE name = ?", (args.name,)
    ).fetchall()
    print(f"{args.name}: {len(direct)} direct call site(s)")
    for path, ctx in sorted(direct)[:20]:
        print(f"  {path}  in {ctx}")
    second: set[tuple] = set()
    for _, ctx in direct:
        if not ctx or ctx == "<module>":
            continue
        for row in conn.execute(
            "SELECT DISTINCT path, ctx FROM refs WHERE name = ?", (ctx,)
        ).fetchall():
            second.add(row)
    second -= set(direct)
    if second:
        print(f"\nsecond hop — callers of those callers ({len(second)}):")
        for path, ctx in sorted(second)[:20]:
            print(f"  {path}  in {ctx}")
    return 0


def cmd_stats(args) -> int:
    conn = _conn()
    for t in ("files", "symbols", "refs", "imports", "routes"):
        print(f"{t:9} {conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]:>7}")
    bad = conn.execute("SELECT path FROM files WHERE lang = 'unparsed'").fetchall()
    if bad:
        print(f"\nunparsed ({len(bad)}) — these are INVISIBLE to every query:")
        for (p,) in bad[:10]:
            print(f"  {p}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="codegraph")
    sub = ap.add_subparsers(dest="cmd", required=True)
    index = sub.add_parser("index", help="build or refresh the index (incremental)")
    index.add_argument(
        "--full", action="store_true", help="discard and rebuild from scratch"
    )
    index.set_defaults(fn=cmd_index)

    for name, fn, help_text in (
        ("def", cmd_def, "where a symbol is defined"),
        ("callers", cmd_callers, "call sites only"),
        ("uses", cmd_uses, "every reference: calls, values, base classes"),
        ("impact", cmd_impact, "callers, and callers of those callers"),
    ):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("name")
        sp.set_defaults(fn=fn)

    imp = sub.add_parser("importers", help="files importing a module")
    imp.add_argument("module")
    imp.set_defaults(fn=cmd_importers)

    rt = sub.add_parser("routes", help="HTTP routes and celery tasks")
    rt.add_argument("pattern", nargs="?")
    rt.set_defaults(fn=cmd_routes)

    st = sub.add_parser("stats", help="index size, and files that failed to parse")
    st.set_defaults(fn=cmd_stats)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
