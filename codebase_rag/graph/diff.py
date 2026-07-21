"""Diff -> changed line ranges -> changed symbols.

Symbol-level seeding is the difference between a usable reviewer and noise.
Seeding a whole file makes every symbol in it an epicenter; on a real repo
that pulled 1393 impacted nodes (truncated) where the symbol-level seed for
the same edit pulled 167.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from loguru import logger

from .store import GraphNode, GraphStore

# Refs reach subprocess, so validate before they ever get there.
_SAFE_GIT_REF = re.compile(r"^[A-Za-z0-9_.~^/@{}\-]+$")

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$")

GIT_TIMEOUT = 30


class UnsafeRefError(ValueError):
    """A ref failed validation and was never passed to git."""


def _validate_ref(ref: str) -> str:
    if not ref or not _SAFE_GIT_REF.match(ref):
        raise UnsafeRefError(f"unsafe git ref: {ref!r}")
    return ref


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(  # noqa: S603 - list args, no shell
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=GIT_TIMEOUT,
        check=False,
    ).stdout


def parse_diff_ranges(diff_text: str) -> dict[str, list[tuple[int, int]]]:
    """Unified diff -> {file: [(start_line, end_line), ...]} on the new side."""
    ranges: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None

    for line in diff_text.splitlines():
        file_match = _FILE_RE.match(line)
        if file_match:
            current = file_match.group(1).replace("\\", "/")
            ranges.setdefault(current, [])
            continue

        hunk = _HUNK_RE.match(line)
        if hunk and current:
            start = int(hunk.group(1))
            count = int(hunk.group(2)) if hunk.group(2) is not None else 1
            if count == 0:
                # Pure deletion: no new lines exist. Anchor on the line the
                # removed block sat against so the surrounding symbol is
                # still implicated.
                ranges[current].append((start, start))
            else:
                ranges[current].append((start, start + count - 1))

    return {path: spans for path, spans in ranges.items() if spans}


# Review what is on disk rather than a commit — the pre-commit case.
WORKTREE = "WORKTREE"


def changed_ranges(
    repo_root: str | Path,
    base: str,
    head: str = "HEAD",
) -> dict[str, list[tuple[int, int]]]:
    """Changed line ranges per file for base...head.

    `head=WORKTREE` compares the working tree against base instead, which is
    what a pre-commit review needs: the changes have not been committed yet,
    so there is no head commit to diff against.
    """
    repo_root = Path(repo_root)
    _validate_ref(base)

    if head == WORKTREE:
        # Uncommitted work, staged and unstaged. The graph is built from the
        # same files on disk, so line numbers line up.
        diff = _git(["diff", "--unified=0", base, "--"], repo_root)
        return parse_diff_ranges(diff)

    _validate_ref(head)
    merge_base = _git(["merge-base", base, head], repo_root).strip() or base
    _validate_ref(merge_base)

    diff = _git(["diff", "--unified=0", f"{merge_base}...{head}", "--"], repo_root)
    return parse_diff_ranges(diff)


def map_ranges_to_nodes(
    store: GraphStore,
    ranges: dict[str, list[tuple[int, int]]],
) -> list[GraphNode]:
    """Symbols whose body overlaps a changed line range.

    File nodes are excluded — they span the whole file and would overlap
    every hunk, which is exactly the over-seeding this function exists to
    prevent.
    """
    seeds: list[GraphNode] = []
    seen: set[str] = set()

    for file_path, spans in ranges.items():
        nodes = store.get_nodes_by_file(file_path)
        if not nodes:
            continue

        matched_any = False
        candidates = [n for n in nodes if n.kind != "File"]
        for start, end in spans:
            covering = [
                n for n in candidates
                if n.line_start <= end and n.line_end >= start
            ]
            if not covering:
                continue
            matched_any = True
            # Seed the innermost symbol only. A change inside a method also
            # falls within its class, and the class spans every method, so
            # seeding both lets the class's much larger reach bury the method
            # that actually changed.
            innermost = min(covering, key=lambda n: n.line_end - n.line_start)
            if innermost.qualified_name not in seen:
                seen.add(innermost.qualified_name)
                seeds.append(innermost)

        if not matched_any:
            # Edits landed outside any symbol — module-level statements,
            # imports, constants. The file itself is the unit of change.
            file_node = next((n for n in nodes if n.kind == "File"), None)
            if file_node and file_node.qualified_name not in seen:
                seen.add(file_node.qualified_name)
                seeds.append(file_node)

    logger.info(f"Mapped {len(ranges)} changed file(s) to {len(seeds)} changed symbol(s)")
    return seeds


def changed_symbols(
    store: GraphStore,
    repo_root: str | Path,
    base: str,
    head: str = "HEAD",
) -> tuple[list[GraphNode], dict[str, list[tuple[int, int]]]]:
    """Convenience: diff a range and resolve it to graph nodes."""
    ranges = changed_ranges(repo_root, base, head)
    return map_ranges_to_nodes(store, ranges), ranges
