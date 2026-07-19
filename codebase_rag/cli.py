"""CLI entry for PR blast-radius review (local + GitHub Actions)."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from loguru import logger

from codebase_rag.services.pocket_router import PocketStrategyRouter
from codebase_rag.services.pr_review.fetcher import RepoFetcher


def _env(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def _resolve_base(repo: Path, base: str | None) -> str:
    if base:
        # Actions often pass the bare branch name (e.g. "main").
        if not base.startswith(("origin/", "refs/")) and _ref_exists(repo, f"origin/{base}"):
            return f"origin/{base}"
        return base

    github_base = _env("GITHUB_BASE_REF")
    if github_base:
        candidate = f"origin/{github_base}"
        if _ref_exists(repo, candidate):
            return candidate
        if _ref_exists(repo, github_base):
            return github_base

    for candidate in ("origin/main", "origin/master", "main", "master"):
        if _ref_exists(repo, candidate):
            return candidate
    return "HEAD~1"


def _ref_exists(repo: Path, ref: str) -> bool:
    result = RepoFetcher()._git(
        ["git", "rev-parse", "--verify", ref],
        cwd=repo,
        check=False,
    )
    return result.returncode == 0


def _severity_rank(severity: str) -> int:
    return {"P0": 0, "P1": 1, "P2": 2, "P3": 3}.get(severity.upper(), 9)


def _worst_severity(report: str) -> str | None:
    matches = re.findall(r"\[(P[0-3])\]", report)
    if not matches:
        return None
    return min(matches, key=_severity_rank)


def _should_fail(report: str, fail_on: str) -> bool:
    if fail_on == "none":
        return False
    worst = _worst_severity(report)
    if worst is None:
        return False
    if fail_on == "any":
        return True
    if fail_on == "p0":
        return worst == "P0"
    if fail_on == "p1":
        return _severity_rank(worst) <= 1
    return False


def build_report(
    repo: Path,
    base: str | None = None,
    head: str = "HEAD",
    engine: str = "graph",
) -> str:
    repo = repo.resolve()
    resolved_base = _resolve_base(repo, base)

    if engine == "graph":
        try:
            return _graph_report(repo, resolved_base, head)
        except Exception as exc:  # noqa: BLE001 - CI must still get a report
            logger.warning(f"graph engine failed ({exc}); falling back to heuristic")

    fetcher = RepoFetcher()
    changes = fetcher.fetch_range_diff(repo, base=resolved_base, head=head)
    router = PocketStrategyRouter()
    return router.route_and_review(
        {"changes": changes, "base": resolved_base, "head": head},
        repo_root=repo,
    )


def _graph_report(repo: Path, base: str, head: str) -> str:
    """Graph-backed review: same findings the web UI and MCP tools produce."""
    from codebase_rag.graph.build import build_graph
    from codebase_rag.graph.review import format_report, review_range
    from codebase_rag.graph.store import default_db_path

    db = default_db_path(repo)
    # Incremental: only re-parses changed files after the first build.
    build_graph(repo, db_path=db)
    return format_report(review_range(repo, base=base, head=head, db_path=db))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="prgraf",
        description="Low-noise blast-radius PR reviewer. Run on every PR upload.",
    )
    parser.add_argument(
        "--repo",
        default=_env("GITHUB_WORKSPACE", "PRGRAF_REPO", default="."),
        help="Path to the git checkout (default: . or GITHUB_WORKSPACE)",
    )
    parser.add_argument(
        "--base",
        default=_env("PRGRAF_BASE", "GITHUB_BASE_REF"),
        help="Base ref/branch (default: GITHUB_BASE_REF or origin/main)",
    )
    parser.add_argument(
        "--head",
        default=_env("PRGRAF_HEAD", default="HEAD"),
        help="Head ref (default: HEAD)",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=_env("PRGRAF_OUTPUT", default="prgraf-report.md"),
        help="Write report markdown here (default: prgraf-report.md)",
    )
    parser.add_argument(
        "--fail-on",
        choices=("none", "p0", "p1", "any"),
        default=_env("PRGRAF_FAIL_ON", default="none"),
        help="Exit non-zero when findings meet this threshold",
    )
    parser.add_argument(
        "--engine",
        choices=("graph", "heuristic"),
        default=_env("PRGRAF_ENGINE", default="graph"),
        help="Review engine: graph (tree-sitter blast radius) or heuristic (regex)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Only print the report (less log noise)",
    )
    args = parser.parse_args(argv)

    if args.quiet:
        logger.remove()

    repo = Path(args.repo)
    if not (repo / ".git").exists() and not (repo / ".git").is_file():
        # Allow worktrees / nested checkouts where .git may be a file
        result = RepoFetcher()._git(["git", "rev-parse", "--git-dir"], cwd=repo, check=False)
        if result.returncode != 0:
            print(f"error: not a git repository: {repo}", file=sys.stderr)
            return 2

    try:
        report = build_report(repo=repo, base=args.base, head=args.head, engine=args.engine)
    except Exception as exc:
        logger.error(f"Review failed: {exc}")
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(report)

    if args.output:
        out = Path(args.output)
        out.write_text(f"## Blast-Radius PR Review\n\n```text\n{report}\n```\n", encoding="utf-8")
        logger.info(f"Wrote {out}")

    if _should_fail(report, args.fail_on):
        logger.error(f"Failing because --fail-on={args.fail_on}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
