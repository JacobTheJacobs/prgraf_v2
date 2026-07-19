"""SQLite-backed code graph.

Two tables, nodes and edges. No graph database, no ORM. The impact
traversal is a bounded best-score relaxation ported from upstream
code-review-graph.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .constants import (
    IMPACT_DEFAULT_EDGE_WEIGHT,
    IMPACT_DEPTH_DECAY,
    IMPACT_EDGE_WEIGHTS,
    IMPACT_SCORE_FLOOR,
    MAX_IMPACT_DEPTH,
    MAX_IMPACT_NODES,
)

SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,              -- File, Class, Function, Type, Test
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL UNIQUE,
    file_path TEXT NOT NULL,
    line_start INTEGER,
    line_end INTEGER,
    language TEXT,
    parent_name TEXT,
    params TEXT,
    is_test INTEGER DEFAULT 0,
    file_hash TEXT,
    extra TEXT DEFAULT '{}',
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,              -- CALLS, IMPORTS_FROM, INHERITS, ...
    source_qualified TEXT NOT NULL,
    target_qualified TEXT NOT NULL,
    file_path TEXT NOT NULL,
    line INTEGER DEFAULT 0,
    confidence REAL DEFAULT 1.0,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    file_hash TEXT NOT NULL,
    language TEXT,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file_path);
CREATE INDEX IF NOT EXISTS idx_nodes_kind ON nodes(kind);
CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_qualified);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_qualified);
CREATE INDEX IF NOT EXISTS idx_edges_file ON edges(file_path);
CREATE INDEX IF NOT EXISTS idx_edges_source_kind ON edges(source_qualified, kind);
CREATE INDEX IF NOT EXISTS idx_edges_target_kind ON edges(target_qualified, kind);
"""


@dataclass
class GraphNode:
    kind: str
    name: str
    qualified_name: str
    file_path: str
    line_start: int = 0
    line_end: int = 0
    language: str = ""
    parent_name: str | None = None
    params: str | None = None
    is_test: bool = False
    file_hash: str | None = None
    extra: dict = field(default_factory=dict)
    id: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "qualified_name": self.qualified_name,
            "file_path": self.file_path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "language": self.language,
            "parent_name": self.parent_name,
            "is_test": self.is_test,
        }


@dataclass
class GraphEdge:
    kind: str
    source_qualified: str
    target_qualified: str
    file_path: str
    line: int = 0
    confidence: float = 1.0
    id: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "source": self.source_qualified,
            "target": self.target_qualified,
            "file_path": self.file_path,
            "line": self.line,
            "confidence": self.confidence,
        }


def _row_to_node(row: sqlite3.Row) -> GraphNode:
    try:
        extra = json.loads(row["extra"] or "{}")
    except (json.JSONDecodeError, TypeError):
        extra = {}
    return GraphNode(
        id=row["id"],
        kind=row["kind"],
        name=row["name"],
        qualified_name=row["qualified_name"],
        file_path=row["file_path"],
        line_start=row["line_start"] or 0,
        line_end=row["line_end"] or 0,
        language=row["language"] or "",
        parent_name=row["parent_name"],
        params=row["params"],
        is_test=bool(row["is_test"]),
        file_hash=row["file_hash"],
        extra=extra,
    )


def _row_to_edge(row: sqlite3.Row) -> GraphEdge:
    return GraphEdge(
        id=row["id"],
        kind=row["kind"],
        source_qualified=row["source_qualified"],
        target_qualified=row["target_qualified"],
        file_path=row["file_path"],
        line=row["line"] or 0,
        confidence=row["confidence"] if row["confidence"] is not None else 1.0,
    )


class GraphStore:
    """SQLite code graph. Single writer, WAL mode, explicit transactions."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path),
            timeout=30,
            check_same_thread=False,
            isolation_level=None,  # explicit transactions
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.execute(
            "INSERT OR IGNORE INTO metadata (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> GraphStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- writes -------------------------------------------------------------

    def replace_file(
        self,
        file_path: str,
        file_hash: str,
        language: str,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
    ) -> None:
        """Atomically swap all nodes/edges belonging to one file."""
        now = time.time()
        conn = self._conn
        conn.execute("BEGIN")
        try:
            conn.execute("DELETE FROM nodes WHERE file_path = ?", (file_path,))
            conn.execute("DELETE FROM edges WHERE file_path = ?", (file_path,))
            if nodes:
                conn.executemany(
                    "INSERT OR REPLACE INTO nodes "
                    "(kind, name, qualified_name, file_path, line_start, line_end, "
                    " language, parent_name, params, is_test, file_hash, extra, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            n.kind, n.name, n.qualified_name, n.file_path,
                            n.line_start, n.line_end, n.language, n.parent_name,
                            n.params, int(n.is_test), file_hash,
                            json.dumps(n.extra), now,
                        )
                        for n in nodes
                    ],
                )
            if edges:
                conn.executemany(
                    "INSERT INTO edges "
                    "(kind, source_qualified, target_qualified, file_path, line, "
                    " confidence, updated_at) VALUES (?,?,?,?,?,?,?)",
                    [
                        (
                            e.kind, e.source_qualified, e.target_qualified,
                            e.file_path, e.line, e.confidence, now,
                        )
                        for e in edges
                    ],
                )
            conn.execute(
                "INSERT OR REPLACE INTO files (path, file_hash, language, updated_at) "
                "VALUES (?,?,?,?)",
                (file_path, file_hash, language, now),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def forget_file(self, file_path: str) -> None:
        conn = self._conn
        conn.execute("BEGIN")
        try:
            conn.execute("DELETE FROM nodes WHERE file_path = ?", (file_path,))
            conn.execute("DELETE FROM edges WHERE file_path = ?", (file_path,))
            conn.execute("DELETE FROM files WHERE path = ?", (file_path,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def known_files(self) -> dict[str, str]:
        """Map of tracked file path -> stored hash."""
        return {
            row["path"]: row["file_hash"]
            for row in self._conn.execute("SELECT path, file_hash FROM files")
        }

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?,?)", (key, value)
        )

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    # -- reads --------------------------------------------------------------

    def get_nodes_by_file(self, file_path: str) -> list[GraphNode]:
        rows = self._conn.execute(
            "SELECT * FROM nodes WHERE file_path = ? ORDER BY line_start", (file_path,)
        ).fetchall()
        return [_row_to_node(r) for r in rows]

    def get_node(self, qualified_name: str) -> GraphNode | None:
        row = self._conn.execute(
            "SELECT * FROM nodes WHERE qualified_name = ?", (qualified_name,)
        ).fetchone()
        return _row_to_node(row) if row else None

    def find_nodes(self, name: str, limit: int = 20) -> list[GraphNode]:
        rows = self._conn.execute(
            "SELECT * FROM nodes WHERE name = ? OR qualified_name LIKE ? "
            "ORDER BY CASE WHEN name = ? THEN 0 ELSE 1 END, qualified_name LIMIT ?",
            (name, f"%{name}%", name, limit),
        ).fetchall()
        return [_row_to_node(r) for r in rows]

    def callers_of(self, qualified_name: str, limit: int = 50) -> list[GraphNode]:
        rows = self._conn.execute(
            "SELECT n.* FROM edges e JOIN nodes n ON n.qualified_name = e.source_qualified "
            "WHERE e.target_qualified = ? AND e.kind = 'CALLS' LIMIT ?",
            (qualified_name, limit),
        ).fetchall()
        return [_row_to_node(r) for r in rows]

    def callees_of(self, qualified_name: str, limit: int = 50) -> list[GraphNode]:
        rows = self._conn.execute(
            "SELECT n.* FROM edges e JOIN nodes n ON n.qualified_name = e.target_qualified "
            "WHERE e.source_qualified = ? AND e.kind = 'CALLS' LIMIT ?",
            (qualified_name, limit),
        ).fetchall()
        return [_row_to_node(r) for r in rows]

    def tests_for(self, qualified_name: str, limit: int = 50) -> list[GraphNode]:
        """Tests covering a node.

        TESTED_BY is stored source=production, target=test, so the test side
        is the target. Getting this backwards silently reports zero coverage.
        """
        rows = self._conn.execute(
            "SELECT n.* FROM edges e JOIN nodes n ON n.qualified_name = e.target_qualified "
            "WHERE e.source_qualified = ? AND e.kind = 'TESTED_BY' LIMIT ?",
            (qualified_name, limit),
        ).fetchall()
        return [_row_to_node(r) for r in rows]

    def edges_between(self, qualified_names: set[str]) -> list[GraphEdge]:
        """Edges with both endpoints inside the given set."""
        if not qualified_names:
            return []
        conn = self._conn
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _edge_scope (qn TEXT PRIMARY KEY)")
        conn.execute("DELETE FROM _edge_scope")
        conn.executemany(
            "INSERT OR IGNORE INTO _edge_scope (qn) VALUES (?)",
            [(qn,) for qn in qualified_names],
        )
        rows = conn.execute(
            "SELECT e.* FROM edges e "
            "JOIN _edge_scope s1 ON s1.qn = e.source_qualified "
            "JOIN _edge_scope s2 ON s2.qn = e.target_qualified"
        ).fetchall()
        return [_row_to_edge(r) for r in rows]

    def stats(self) -> dict[str, Any]:
        conn = self._conn
        nodes_by_kind = {
            row["kind"]: row["c"]
            for row in conn.execute("SELECT kind, COUNT(*) c FROM nodes GROUP BY kind")
        }
        edges_by_kind = {
            row["kind"]: row["c"]
            for row in conn.execute("SELECT kind, COUNT(*) c FROM edges GROUP BY kind")
        }
        languages = [
            row["language"]
            for row in conn.execute(
                "SELECT DISTINCT language FROM files WHERE language IS NOT NULL "
                "AND language != '' ORDER BY language"
            )
        ]
        return {
            "total_nodes": sum(nodes_by_kind.values()),
            "total_edges": sum(edges_by_kind.values()),
            "nodes_by_kind": nodes_by_kind,
            "edges_by_kind": edges_by_kind,
            "files": conn.execute("SELECT COUNT(*) c FROM files").fetchone()["c"],
            "languages": languages,
            "last_build": self.get_meta("last_build"),
        }

    def _batch_get_nodes(self, qualified_names: set[str]) -> list[GraphNode]:
        if not qualified_names:
            return []
        conn = self._conn
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _node_fetch (qn TEXT PRIMARY KEY)")
        conn.execute("DELETE FROM _node_fetch")
        conn.executemany(
            "INSERT OR IGNORE INTO _node_fetch (qn) VALUES (?)",
            [(qn,) for qn in qualified_names],
        )
        rows = conn.execute(
            "SELECT n.* FROM nodes n JOIN _node_fetch f ON f.qn = n.qualified_name"
        ).fetchall()
        return [_row_to_node(r) for r in rows]

    # -- impact radius ------------------------------------------------------

    def get_impact_radius(
        self,
        changed_files: list[str] | None = None,
        seed_qualified_names: set[str] | None = None,
        max_depth: int = MAX_IMPACT_DEPTH,
        max_nodes: int = MAX_IMPACT_NODES,
    ) -> dict[str, Any]:
        """Blast radius from a set of changed files or seed symbols.

        Bounded best-score relaxation rather than path enumeration: dense
        cyclic graphs contain exponentially many paths, so instead we keep
        exactly one best score per node across three temp tables and scan
        the edge table once per depth level.

        Traversal is bidirectional — a change reaches both its callers and
        its callees, and both are part of what a reviewer must check.
        """
        max_depth = max(0, int(max_depth))
        max_nodes = max(0, int(max_nodes))

        empty = {
            "changed_nodes": [],
            "impacted_nodes": [],
            "impacted_files": [],
            "edges": [],
            "truncated": False,
            "total_impacted": 0,
            "impact_scores": {},
        }

        seeds: set[str] = set(seed_qualified_names or ())
        for path in changed_files or ():
            for node in self.get_nodes_by_file(path):
                seeds.add(node.qualified_name)
        if not seeds:
            return empty

        conn = self._conn
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _impact_seeds (qn TEXT PRIMARY KEY)")
        conn.execute("DELETE FROM _impact_seeds")
        conn.executemany(
            "INSERT OR IGNORE INTO _impact_seeds (qn) VALUES (?)", [(s,) for s in seeds]
        )

        conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS _impact_weights "
            "(kind TEXT PRIMARY KEY, weight REAL NOT NULL)"
        )
        conn.execute("DELETE FROM _impact_weights")
        conn.executemany(
            "INSERT INTO _impact_weights (kind, weight) VALUES (?,?)",
            list(IMPACT_EDGE_WEIGHTS.items()),
        )
        for table in ("_impact_best", "_impact_frontier", "_impact_next"):
            conn.execute(
                f"CREATE TEMP TABLE IF NOT EXISTS {table} "  # noqa: S608 - fixed names
                "(node_qn TEXT PRIMARY KEY, score REAL NOT NULL)"
            )
            conn.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed names

        conn.execute("INSERT INTO _impact_best (node_qn, score) SELECT qn, 1.0 FROM _impact_seeds")
        conn.execute(
            "INSERT INTO _impact_frontier (node_qn, score) SELECT qn, 1.0 FROM _impact_seeds"
        )

        candidate_sql = """
        INSERT INTO _impact_next (node_qn, score)
        SELECT node_qn, MAX(score) FROM (
            SELECT e.target_qualified AS node_qn,
                   f.score * COALESCE(w.weight, ?) * ? AS score
            FROM _impact_frontier f
            JOIN edges e ON e.source_qualified = f.node_qn
            LEFT JOIN _impact_weights w ON w.kind = e.kind
            UNION ALL
            SELECT e.source_qualified AS node_qn,
                   f.score * COALESCE(w.weight, ?) * ? AS score
            FROM _impact_frontier f
            JOIN edges e ON e.target_qualified = f.node_qn
            LEFT JOIN _impact_weights w ON w.kind = e.kind
        ) candidates
        WHERE score > ?
        GROUP BY node_qn
        """
        params = (
            IMPACT_DEFAULT_EDGE_WEIGHT, IMPACT_DEPTH_DECAY,
            IMPACT_DEFAULT_EDGE_WEIGHT, IMPACT_DEPTH_DECAY,
            IMPACT_SCORE_FLOOR,
        )

        for _ in range(max_depth):
            conn.execute("DELETE FROM _impact_next")
            conn.execute(candidate_sql, params)
            # Only keep strict improvements, otherwise cycles never settle.
            conn.execute(
                "DELETE FROM _impact_next WHERE score <= COALESCE("
                "(SELECT score FROM _impact_best b WHERE b.node_qn = _impact_next.node_qn), 0.0)"
            )
            if conn.execute("SELECT 1 FROM _impact_next LIMIT 1").fetchone() is None:
                break
            conn.execute(
                "INSERT OR REPLACE INTO _impact_best (node_qn, score) "
                "SELECT node_qn, score FROM _impact_next"
            )
            conn.execute("DELETE FROM _impact_frontier")
            conn.execute(
                "INSERT INTO _impact_frontier (node_qn, score) "
                "SELECT node_qn, score FROM _impact_next"
            )

        # Fetch one past the cap as a truncation sentinel. Joining nodes drops
        # unresolved endpoints that acted as bridges but aren't real symbols.
        rows = conn.execute(
            "SELECT b.node_qn, b.score FROM _impact_best b "
            "JOIN nodes n ON n.qualified_name = b.node_qn "
            "LEFT JOIN _impact_seeds s ON s.qn = b.node_qn "
            "WHERE s.qn IS NULL "
            "ORDER BY b.score DESC, b.node_qn LIMIT ?",
            (max_nodes + 1,),
        ).fetchall()

        truncated = len(rows) > max_nodes
        if truncated:
            total_impacted = conn.execute(
                "SELECT COUNT(*) c FROM _impact_best b "
                "JOIN nodes n ON n.qualified_name = b.node_qn "
                "LEFT JOIN _impact_seeds s ON s.qn = b.node_qn "
                "WHERE s.qn IS NULL"
            ).fetchone()["c"]
        else:
            total_impacted = len(rows)

        kept = rows[:max_nodes]
        score_by_qn = {r["node_qn"]: float(r["score"]) for r in kept}

        changed_nodes = self._batch_get_nodes(seeds)
        impacted_nodes = self._batch_get_nodes(set(score_by_qn))
        impacted_nodes.sort(
            key=lambda n: (-score_by_qn.get(n.qualified_name, 0.0), n.qualified_name)
        )

        scope = seeds | set(score_by_qn)
        impacted_files = sorted({n.file_path for n in impacted_nodes})

        return {
            "changed_nodes": changed_nodes,
            "impacted_nodes": impacted_nodes,
            "impacted_files": impacted_files,
            "edges": self.edges_between(scope),
            "truncated": truncated,
            "total_impacted": total_impacted,
            "impact_scores": {qn: round(s, 4) for qn, s in score_by_qn.items()},
        }


def default_db_path(repo_root: str | Path) -> Path:
    from .constants import DB_DIRNAME, DB_FILENAME

    return Path(repo_root) / DB_DIRNAME / DB_FILENAME
