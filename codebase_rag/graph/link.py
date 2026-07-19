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


def link_graph(store: GraphStore) -> dict[str, int]:
    """Rewrite unresolved edge targets in place. Returns resolution counts."""
    conn = store._conn  # noqa: SLF001 - same package, intentional

    # name -> [(qualified_name, file_path)]
    by_name: dict[str, list[tuple[str, str]]] = defaultdict(list)
    # import spelling -> file qualified name
    by_module: dict[str, str] = {}

    for row in conn.execute("SELECT qualified_name, name, file_path, kind FROM nodes"):
        if row["kind"] == "File":
            for variant in _module_variants(row["file_path"]):
                by_module.setdefault(variant, row["qualified_name"])
        else:
            by_name[row["name"]].append((row["qualified_name"], row["file_path"]))

    resolved = 0
    dropped = 0
    updates: list[tuple[str, float, int]] = []
    deletions: list[tuple[int]] = []

    rows = conn.execute(
        "SELECT id, kind, source_qualified, target_qualified, file_path FROM edges "
        f"WHERE kind IN ({','.join('?' * len(_RESOLVABLE))})",
        _RESOLVABLE,
    ).fetchall()

    for row in rows:
        target = row["target_qualified"]
        # Already qualified (contains the file::symbol separator).
        if "::" in target:
            continue

        candidates = by_name.get(target)
        if not candidates:
            deletions.append((row["id"],))
            dropped += 1
            continue

        source_file = row["file_path"]
        source_dir = str(Path(source_file).parent)

        if len(candidates) == 1:
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

        updates.append((chosen, confidence, row["id"]))
        resolved += 1

    # Imports resolve against the file index instead of the symbol index.
    import_rows = conn.execute(
        "SELECT id, target_qualified, file_path FROM edges WHERE kind = 'IMPORTS_FROM'"
    ).fetchall()
    imports_resolved = 0
    for row in import_rows:
        target = row["target_qualified"]
        if target in by_module:
            hit = by_module[target]
        else:
            normalized = target.replace("\\", "/").lstrip("./")
            hit = by_module.get(normalized)
            if hit is None:
                for ext in (".py", ".ts", ".tsx", ".js", ".jsx", ".go"):
                    hit = by_module.get(normalized + ext)
                    if hit:
                        break
        if hit is None:
            deletions.append((row["id"],))  # third-party / stdlib: out of scope
            dropped += 1
            continue
        updates.append((hit, 0.9, row["id"]))
        imports_resolved += 1

    conn.execute("BEGIN")
    try:
        if updates:
            conn.executemany(
                "UPDATE edges SET target_qualified = ?, confidence = ? WHERE id = ?",
                updates,
            )
        if deletions:
            conn.executemany("DELETE FROM edges WHERE id = ?", deletions)
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
    if not rows:
        return 0

    import time

    now = time.time()
    conn.execute("BEGIN")
    try:
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
