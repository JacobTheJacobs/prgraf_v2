"""Walk a repo, extract symbols, store the graph.

Full build and incremental update share one path; the only difference is
which files get re-parsed. Content hashes decide, not mtimes.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from loguru import logger

from .constants import MAX_FILE_BYTES, SKIP_DIRS
from .extract import extract_file, language_for
from .link import link_graph
from .store import GraphStore, default_db_path


def iter_source_files(repo_root: Path) -> list[Path]:
    """Every parseable file under repo_root, skipping vendored/build dirs."""
    found: list[Path] = []
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if language_for(path) is None:
            continue
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
        seen: set[str] = set()
        parsed = 0
        skipped = 0

        for path in on_disk:
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
