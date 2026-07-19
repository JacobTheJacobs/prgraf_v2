"""Walk a repo, extract symbols, store the graph.

Full build and incremental update share one path; the only difference is
which files get re-parsed. Content hashes decide, not mtimes.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

from loguru import logger

from .constants import MAX_FILE_BYTES, MAX_INDEX_FILES, SKIP_DIRS
from .extract import extract_file, language_for
from .link import link_graph
from .store import GraphStore, default_db_path


class ScopeTooLargeError(RuntimeError):
    """The target looks like a folder of many projects, not one repo."""


def iter_source_files(repo_root: Path) -> list[Path]:
    """Every parseable file under repo_root, skipping vendored/build dirs.

    Prunes directories during the walk rather than filtering after the fact:
    rglob("*") descends into node_modules/.git/.venv and only then discards
    them, which on a big tree costs tens of seconds before any parsing starts.
    """
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        # in-place mutation is what actually prunes the walk
        dirnames[:] = [
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
        ]
        for filename in filenames:
            path = Path(dirpath) / filename
            if language_for(path) is not None:
                found.append(path)
    return found


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def build_graph(
    repo_root: str | Path,
    db_path: str | Path | None = None,
    *,
    full: bool = False,
) -> dict:
    """Build or incrementally update the graph. Returns build stats."""
    repo_root = Path(repo_root).resolve()
    store = GraphStore(db_path or default_db_path(repo_root))
    started = time.time()

    try:
        known = {} if full else store.known_files()
        if full:
            store._conn.executescript(  # noqa: SLF001
                "DELETE FROM nodes; DELETE FROM edges; DELETE FROM files;"
            )

        on_disk = iter_source_files(repo_root)
        if len(on_disk) > MAX_INDEX_FILES:
            raise ScopeTooLargeError(
                f"{repo_root} holds {len(on_disk)} source files "
                f"(limit {MAX_INDEX_FILES}).\n"
                "This usually means the folder contains several projects rather "
                "than one repository — open the specific project instead.\n"
                "To index it anyway, set PRGRAF_MAX_INDEX_FILES higher."
            )

        seen: set[str] = set()
        parsed = 0
        skipped = 0
        total = len(on_disk)

        for index, path in enumerate(on_disk, start=1):
            if total > 400 and index % 250 == 0:
                logger.info(f"parsing {index}/{total} files…")
            rel = path.relative_to(repo_root).as_posix()
            seen.add(rel)
            try:
                # Read bytes once, then hash — no TOCTOU window between the
                # hash we store and the content we actually parsed.
                data = path.read_bytes()
            except OSError as exc:
                logger.warning(f"unreadable {rel}: {exc}")
                continue

            if len(data) > MAX_FILE_BYTES:
                skipped += 1
                continue

            file_hash = _hash_bytes(data)
            if known.get(rel) == file_hash:
                continue

            language = language_for(path)
            assert language is not None  # iter_source_files filtered these
            nodes, edges = extract_file(rel, data, language)
            store.replace_file(rel, file_hash, language, nodes, edges)
            parsed += 1

        removed = 0
        for gone in set(known) - seen:
            store.forget_file(gone)
            removed += 1

        link_graph(store)

        elapsed = time.time() - started
        store.set_meta("last_build", time.strftime("%Y-%m-%dT%H:%M:%S"))
        stats = store.stats()
        stats.update({
            "parsed": parsed,
            "removed": removed,
            "skipped_large": skipped,
            "elapsed_sec": round(elapsed, 2),
        })
        logger.info(
            f"Graph: {stats['total_nodes']} nodes, {stats['total_edges']} edges "
            f"from {stats['files']} file(s) in {stats['elapsed_sec']}s "
            f"({parsed} parsed, {removed} removed)"
        )
        return stats
    finally:
        store.close()
