"""Tunables for graph traversal and impact scoring.

Weights and decay are ported from code-review-graph (upstream). They are
review-risk weights: how likely is a change at one end to break the other.
"""

from __future__ import annotations

import math
import os

DB_DIRNAME = ".prgraf"
DB_FILENAME = "graph.db"


def _bounded_float_env(name: str, default: float, *, lower: float, upper: float) -> float:
    """Read a finite float strictly inside (lower, upper).

    Bad config falls back to the default rather than making traversal
    unbounded or exploding at import time.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value) or not lower < value < upper:
        return default
    return value


def _int_env(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


# --- Traversal limits ------------------------------------------------------

MAX_IMPACT_NODES = _int_env("PRGRAF_MAX_IMPACT_NODES", 500)
MAX_IMPACT_DEPTH = _int_env("PRGRAF_MAX_IMPACT_DEPTH", 2)

# --- Impact scoring --------------------------------------------------------
# Each hop multiplies the running score, so strongly coupled nodes rank first
# and weak edge types die out within a hop or two.

IMPACT_EDGE_WEIGHTS: dict[str, float] = {
    "CALLS": 1.0,
    "INHERITS": 0.9,
    "IMPLEMENTS": 0.9,
    "TESTED_BY": 0.7,
    "REFERENCES": 0.6,
    "IMPORTS_FROM": 0.5,
    "CONTAINS": 0.3,
}
IMPACT_DEFAULT_EDGE_WEIGHT = 0.5
IMPACT_DEPTH_DECAY = _bounded_float_env(
    "PRGRAF_IMPACT_DEPTH_DECAY", 0.6, lower=0.0, upper=1.0
)
IMPACT_SCORE_FLOOR = _bounded_float_env(
    "PRGRAF_IMPACT_SCORE_FLOOR", 0.05, lower=0.0, upper=1.0
)

NODE_KINDS = ("File", "Class", "Function", "Type", "Test")

SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "env", "__pycache__",
    "node_modules", "dist", "build", "target", "vendor", ".next",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".prgraf",
    "temp_repos", "site-packages",
})

MAX_FILE_BYTES = _int_env("PRGRAF_MAX_FILE_BYTES", 1_500_000)

# Refuse to index an absurdly broad scope. Pointing at a folder that holds many
# unrelated projects (or a home dir) is almost always a mistake, and grinding
# through it silently looks like a hang. Raise the ceiling deliberately with
# PRGRAF_MAX_INDEX_FILES if you really do have one huge repo.
MAX_INDEX_FILES = _int_env("PRGRAF_MAX_INDEX_FILES", 3000)
