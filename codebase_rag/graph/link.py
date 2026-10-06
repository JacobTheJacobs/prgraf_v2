"""Resolve unqualified edge targets into real graph edges.

Extraction records call targets as written (`foo()` -> "foo"). This pass runs
once the whole repo is in the store and rewrites those into qualified names.

This is the highest-leverage code in the build: get it wrong and the graph is
either empty (nothing resolves) or a hairball (everything resolves to
everything). Upstream does the equivalent at render time in
visualization.py:28-99; doing it at build time means the VSCode extension and
MCP tools both read an already-resolved graph.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from loguru import logger

from .extract import RECEIVER_CALL_CONFIDENCE
from .store import GraphStore

# Unqualified targets are only worth resolving via these edge kinds.
_RESOLVABLE = ("CALLS", "INHERITS")


def _module_variants(file_path: str) -> list[str]:
    """Import spellings that could name this file."""
    path = Path(file_path)
    stem_path = path.with_suffix("")
    parts = stem_path.parts
    variants = [file_path, str(stem_path).replace("\\", "/")]
    # Dotted module path (python) and every path suffix (js/go relative imports).
    if parts:
        variants.append(".".join(parts))
        for i in range(len(parts)):
            suffix = "/".join(parts[i:])
            variants.append(suffix)
            variants.append(f"./{suffix}")
            variants.append(f"../{suffix}")
        if parts[-1] in ("index", "__init__"):
            pkg = "/".join(parts[:-1])
            if pkg:
                variants.extend([pkg, f"./{pkg}", ".".join(parts[:-1])])
    return variants


def _module_aliases(file_path: str) -> set[str]:
    """Names a file is called by at a call site: `auth.verify()`.

    The file's stem (Python module, JS file), its directory (Go package, and a
    Python package via `__init__`).
    """
    path = Path(file_path)
    aliases = {path.stem}
    if path.parent.name and (path.suffix == ".go" or path.stem in ("__init__", "index")):
        aliases.add(path.parent.name)
    return aliases


def _resolve_import(
    target: str,
    by_module: dict[str, str],
    by_package: dict[str, list[str]],
) -> list[str]:
    """File qualified names an import spelling refers to (empty if external)."""
    if target in by_module:
        return [by_module[target]]
    normalized = target.replace("\\", "/").lstrip("./")
    if normalized in by_module:
        return [by_module[normalized]]
    for ext in (".py", ".ts", ".tsx", ".js", ".jsx", ".go"):
        if normalized + ext in by_module:
            return [by_module[normalized + ext]]
    # A Go import names a package directory by its full module path
    # (`github.com/org/proj/auth`); match the longest in-repo suffix.
    parts = normalized.split("/")
    for i in range(len(parts)):
        files = by_package.get("/".join(parts[i:]))
        if files:
            return files
    return []


def link_graph(store: GraphStore) -> dict[str, int]:
    """Rewrite unresolved edge targets in place. Returns resolution counts."""
    conn = store._conn  # noqa: SLF001 - same package, intentional

    # name -> [(qualified_name, file_path, is_method)]
    by_name: dict[str, list[tuple[str, str, bool]]] = defaultdict(list)
    # import spelling -> file qualified name
    by_module: dict[str, str] = {}
    # Go package directory (and each path suffix of it) -> its .go files
    by_package: dict[str, list[str]] = defaultdict(list)
    # qualified_name -> kind, so we can tell a class's method from a closure
    # that merely happens to sit inside another function.
    kind_by_qn: dict[str, str] = {}
    pending: list[tuple[str, str, str, str | None]] = []

    for row in conn.execute(
        "SELECT qualified_name, name, file_path, kind, parent_name FROM nodes"
    ):
        kind_by_qn[row["qualified_name"]] = row["kind"]
        if row["kind"] == "File":
            for variant in _module_variants(row["file_path"]):
                by_module.setdefault(variant, row["qualified_name"])
            if row["file_path"].endswith(".go"):
                dir_parts = Path(row["file_path"]).parent.parts
                for i in range(len(dir_parts)):
                    by_package["/".join(dir_parts[i:])].append(row["qualified_name"])
        else:
            pending.append(
                (row["qualified_name"], row["name"], row["file_path"], row["parent_name"])
            )

    for qn, name, file_path, parent_name in pending:
        # A real method is declared inside a class (or, in Go, on a type). A
        # nested arrow function is not, even though both carry a parent_name.
        parent_qn = f"{file_path}::{parent_name}" if parent_name else None
        is_method = bool(parent_qn) and kind_by_qn.get(parent_qn) in ("Class", "Type")
        by_name[name].append((qn, file_path, is_method))

    now = __import__("time").time()
    conn.execute("BEGIN")
    try:
        # Calls that matched nothing on an earlier build go back into play when
        # a symbol of that name now exists. Otherwise an unchanged caller never
        # links to a function added later, and incremental != full build.
        revive = [
            r for r in conn.execute(
                "SELECT rowid, source_qualified, name, file_path, line, on_receiver, receiver "
                "FROM unresolved"
            ).fetchall()
            if r["name"] in by_name
        ]
        if revive:
            conn.executemany(
                "INSERT INTO edges (kind, source_qualified, target_qualified, file_path, "
                "line, confidence, receiver, updated_at) VALUES ('CALLS',?,?,?,?,?,?,?)",
                [
                    (
                        r["source_qualified"], r["name"], r["file_path"], r["line"],
                        RECEIVER_CALL_CONFIDENCE if r["on_receiver"] else 0.5,
                        r["receiver"], now,
                    )
                    for r in revive
                ],
            )
            conn.executemany("DELETE FROM unresolved WHERE rowid = ?", [(r["rowid"],) for r in revive])
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    resolved = 0
    dropped = 0
    updates: list[tuple[str, float, int, int]] = []
    deletions: list[tuple[int]] = []
    unresolved: list[tuple[str, str, str, int, int, str | None]] = []

    # Imports first: a call on a module (`auth.verify()`) binds through them.
    imports_by_file: dict[str, set[str]] = defaultdict(set)
    import_rows = conn.execute(
        "SELECT id, target_qualified, file_path FROM edges WHERE kind = 'IMPORTS_FROM'"
    ).fetchall()
    imports_resolved = 0
    import_updates: list[tuple[str, float, int]] = []
    for row in import_rows:
        target = row["target_qualified"]
        if target in kind_by_qn and kind_by_qn[target] == "File":
            imports_by_file[row["file_path"]].add(target)  # resolved earlier
            continue
        hits = _resolve_import(target, by_module, by_package)
        if not hits:
            deletions.append((row["id"],))  # third-party / stdlib: out of scope
            dropped += 1
            continue
        imports_by_file[row["file_path"]].update(hits)
        import_updates.append((hits[0], 0.9, row["id"]))
        imports_resolved += 1

    files_by_dir: dict[str, list[str]] = defaultdict(list)
    for qn, kind in kind_by_qn.items():
        if kind == "File":
            files_by_dir[Path(qn).parent.as_posix()].append(qn)

    # module alias -> files, per importing file
    alias_files: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for importer, targets in imports_by_file.items():
        for target in targets:
            for alias in _module_aliases(target):
                alias_files[importer][alias].add(target)
            # `from pkg import mod` names the package; `mod` lives inside it.
            if Path(target).stem in ("__init__", "index"):
                for qn in files_by_dir[Path(target).parent.as_posix()]:
                    alias_files[importer][Path(qn).stem].add(qn)

    rows = conn.execute(
        "SELECT id, kind, source_qualified, target_qualified, file_path, line, "
        "confidence, receiver, on_receiver "
        f"FROM edges WHERE kind IN ({','.join('?' * len(_RESOLVABLE))})",
        _RESOLVABLE,
    ).fetchall()

    for row in rows:
        target = row["target_qualified"]
        if "::" in target:
            if target in kind_by_qn:
                continue  # already resolved, and the target still exists
            # Resolved on an earlier build to a symbol that has since been
            # deleted or moved. An incremental build only re-links the files
            # it re-parsed, so without this the caller keeps pointing at a
            # ghost and the graph diverges from what a full build produces.
            # Whether it was a receiver call was recorded at first resolution.
            target = target.rsplit("::", 1)[-1].rsplit(".", 1)[-1]
            on_receiver = bool(row["on_receiver"])
        else:
            on_receiver = (row["confidence"] or 0) <= RECEIVER_CALL_CONFIDENCE

        candidates = by_name.get(target)
        if not candidates:
            deletions.append((row["id"],))
            if row["kind"] == "CALLS":
                unresolved.append((
                    row["source_qualified"], target, row["file_path"],
                    row["line"] or 0, int(on_receiver), row["receiver"],
                ))
            dropped += 1
            continue

        source_file = row["file_path"]
        source_dir = str(Path(source_file).parent)

        if on_receiver:
            # A call on an imported module or Go package: `auth.verify()`.
            # The receiver names the module, so the binding is near-certain.
            module_files = alias_files.get(source_file, {}).get(row["receiver"] or "", set())
            in_module = [c for c in candidates if c[1] in module_files and not c[2]]
            # `x.foo()` otherwise — the receiver's type is unknown, so the only
            # safe bindings are a symbol in this same file, or a genuine method
            # of a class elsewhere. Binding to an unrelated top-level function
            # or a closure nested in some other function invents call graphs:
            # `classList.add(...)` was resolving to a local `const add = ...`
            # in a different file purely because the names matched.
            same_file = [c for c in candidates if c[1] == source_file]
            methods = [c for c in candidates if c[2]]
            if in_module:
                chosen, confidence = sorted(in_module)[0][0], 0.85
            elif same_file:
                chosen, confidence = same_file[0][0], 0.7
            elif len(methods) == 1:
                chosen, confidence = methods[0][0], 0.6
            elif methods:
                chosen, confidence = sorted(methods)[0][0], 0.3
            else:
                deletions.append((row["id"],))
                if row["kind"] == "CALLS":
                    unresolved.append((
                        row["source_qualified"], target, row["file_path"],
                        row["line"] or 0, 1, row["receiver"],
                    ))
                dropped += 1
                continue
        elif len(candidates) == 1:
            chosen, confidence = candidates[0][0], 0.9
        else:
            same_file = [c for c in candidates if c[1] == source_file]
            same_dir = [c for c in candidates if str(Path(c[1]).parent) == source_dir]
            if same_file:
                chosen, confidence = same_file[0][0], 0.8
            elif same_dir:
                chosen, confidence = same_dir[0][0], 0.6
            else:
                # Ambiguous across the repo. Keep it, but say so — a low
                # confidence edge still carries real review signal.
                chosen, confidence = sorted(candidates)[0][0], 0.3

        if chosen == row["source_qualified"]:
            deletions.append((row["id"],))  # self-recursion adds no blast radius
            dropped += 1
            continue

        updates.append((chosen, confidence, int(on_receiver), row["id"]))
        resolved += 1

    conn.execute("BEGIN")
    try:
        if updates:
            conn.executemany(
                "UPDATE edges SET target_qualified = ?, confidence = ?, on_receiver = ? "
                "WHERE id = ?",
                updates,
            )
        if import_updates:
            conn.executemany(
                "UPDATE edges SET target_qualified = ?, confidence = ? WHERE id = ?",
                import_updates,
            )
        if deletions:
            conn.executemany("DELETE FROM edges WHERE id = ?", deletions)
        if unresolved:
            conn.executemany(
                "INSERT INTO unresolved "
                "(source_qualified, name, file_path, line, on_receiver, receiver) "
                "VALUES (?,?,?,?,?,?)",
                unresolved,
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    tested = link_tests(store)

    logger.info(
        f"Linked {resolved} call/inherit, {imports_resolved} import, "
        f"{tested} test edge(s); dropped {dropped} unresolvable"
    )
    return {
        "resolved": resolved,
        "imports_resolved": imports_resolved,
        "tested_by": tested,
        "dropped": dropped,
    }


def link_tests(store: GraphStore) -> int:
    """Derive TESTED_BY from calls made by test symbols.

    Direction matters: source is the production symbol, target is the test.
    Storing it the other way makes coverage queries silently return nothing.
    """
    conn = store._conn  # noqa: SLF001

    rows = conn.execute(
        "SELECT DISTINCT e.target_qualified AS prod, e.source_qualified AS test "
        "FROM edges e "
        "JOIN nodes tn ON tn.qualified_name = e.source_qualified "
        "JOIN nodes pn ON pn.qualified_name = e.target_qualified "
        "WHERE e.kind = 'CALLS' AND tn.is_test = 1 AND pn.is_test = 0"
    ).fetchall()

    import time

    now = time.time()
    conn.execute("BEGIN")
    try:
        # Always clear first: when the last test stops calling something, its
        # old TESTED_BY edge must go too, or coverage outlives the test.
        conn.execute("DELETE FROM edges WHERE kind = 'TESTED_BY'")
        conn.executemany(
            "INSERT INTO edges (kind, source_qualified, target_qualified, file_path, "
            "line, confidence, updated_at) VALUES ('TESTED_BY', ?, ?, '', 0, 0.8, ?)",
            [(r["prod"], r["test"], now) for r in rows],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(rows)
