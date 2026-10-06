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

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_FILE_RE = re.compile(r"^\+\+\+ (?:b/(.+)|/dev/null)$")
_OLD_FILE_RE = re.compile(r"^--- (?:a/(.+)|/dev/null)$")

GIT_TIMEOUT = 30


class UnsafeRefError(ValueError):
    """A ref failed validation and was never passed to git."""


def _validate_ref(ref: str) -> str:
    if not ref or not _SAFE_GIT_REF.match(ref):
        raise UnsafeRefError(f"unsafe git ref: {ref!r}")
    return ref


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(  # noqa: S603 - list args, no shell
        # quotePath off: otherwise a non-ASCII path arrives as "b/\327\251..."
        # and never matches the graph's file paths.
        ["git", "-c", "core.quotePath=false", *args],
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
    """Unified diff -> {file: [(start_line, end_line), ...]} on the new side.

    A pure deletion has no new lines. It is recorded as (n + 1, n), where n is
    the line the removed block sat after: that overlaps only a symbol spanning
    both sides of the gap, i.e. one the lines were removed from the inside of.
    A symbol that merely ends or starts next to the gap is not implicated.
    """
    ranges: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None

    for line in diff_text.splitlines():
        file_match = _FILE_RE.match(line)
        if file_match:
            # `+++ /dev/null` is a deleted file: no new side to map onto.
            current = file_match.group(1)
            if current:
                current = current.replace("\\", "/")
                ranges.setdefault(current, [])
            continue

        hunk = _HUNK_RE.match(line)
        if hunk and current:
            start = int(hunk.group(3))
            count = int(hunk.group(4)) if hunk.group(4) is not None else 1
            if count == 0:
                ranges[current].append((start + 1, start))
            else:
                ranges[current].append((start, start + count - 1))

    return {path: spans for path, spans in ranges.items() if spans}


def parse_removed_ranges(diff_text: str) -> list[tuple[str, str | None, list[tuple[int, int]]]]:
    """Unified diff -> [(old_path, new_path, removed old-side ranges)].

    new_path is None for a deleted file, and differs from old_path on a rename.
    Added files have no old side and are not listed.
    """
    out: list[tuple[str, str | None, list[tuple[int, int]]]] = []
    old_path: str | None = None
    entry: tuple[str, str | None, list[tuple[int, int]]] | None = None

    for line in diff_text.splitlines():
        old_match = _OLD_FILE_RE.match(line)
        if old_match:
            old_path = old_match.group(1)
            entry = None
            continue
        new_match = _FILE_RE.match(line)
        if new_match:
            if old_path:
                entry = (old_path, new_match.group(1), [])
                out.append(entry)
            old_path = None
            continue
        hunk = _HUNK_RE.match(line)
        if hunk and entry:
            start = int(hunk.group(1))
            count = int(hunk.group(2)) if hunk.group(2) is not None else 1
            if count:
                entry[2].append((start, start + count - 1))

    return [e for e in out if e[2]]


# Review what is on disk rather than a commit — the pre-commit case.
WORKTREE = "WORKTREE"


def _diff_text(repo_root: Path, base: str, head: str) -> tuple[str, str]:
    """(diff, old-side commit) for base...head, or base vs the working tree."""
    _validate_ref(base)

    if head == WORKTREE:
        # Uncommitted work, staged and unstaged. The graph is built from the
        # same files on disk, so line numbers line up.
        return _git(["diff", "--unified=0", "-M", base, "--"], repo_root), base

    _validate_ref(head)
    merge_base = _git(["merge-base", base, head], repo_root).strip() or base
    _validate_ref(merge_base)
    diff = _git(["diff", "--unified=0", "-M", f"{merge_base}...{head}", "--"], repo_root)
    return diff, merge_base


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
    diff, _ = _diff_text(Path(repo_root), base, head)
    return parse_diff_ranges(diff)


def removed_symbols(
    store: GraphStore,
    repo_root: str | Path,
    base: str,
    head: str = "HEAD",
) -> list[GraphNode]:
    """Symbols that existed on the old side and are gone on the new one.

    The graph only knows the new side, so a deleted function leaves no node to
    seed — yet its callers are the most certain breakage a PR can cause. The
    old version of each touched file is re-parsed from git to find them.
    """
    from .extract import extract_file, language_for

    repo_root = Path(repo_root)
    diff, old_commit = _diff_text(repo_root, base, head)
    gone: list[GraphNode] = []

    for old_path, new_path, removed in parse_removed_ranges(diff):
        language = language_for(old_path)
        if language is None:
            continue
        source = _git(["show", f"{old_commit}:{old_path}"], repo_root)
        if not source:
            continue
        old_nodes, _ = extract_file(old_path, source.encode("utf-8"), language)
        surviving = {
            (n.name, n.parent_name) for n in store.get_nodes_by_file(new_path)
        } if new_path else set()

        classes = {n.name for n in old_nodes if n.kind == "Class"}
        for node in old_nodes:
            if node.kind == "File":
                continue
            if not any(node.line_start <= end and node.line_end >= start for start, end in removed):
                continue
            # Same name in the same (possibly renamed) file: edited, not removed.
            if (node.name, node.parent_name) in surviving:
                continue
            node.extra = {"deleted": True, "method": node.parent_name in classes}
            gone.append(node)

    if gone:
        logger.info(f"Found {len(gone)} removed symbol(s)")
    return gone


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
