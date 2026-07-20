"""CLI entry for PR blast-radius review (local + GitHub Actions)."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

from loguru import logger

from codebase_rag.graph.build import build_graph
from codebase_rag.graph.diff import WORKTREE
from codebase_rag.graph.review import format_report, review_range
from codebase_rag.graph.store import default_db_path


def _env(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def _resolve_base(repo: Path, base: str | None) -> str:
    if base:
        if base.startswith(("origin/", "refs/")):
            return base
        # Take the ref as written when it already resolves. Prefixing blindly
        # turned "HEAD~1" into "origin/HEAD~1" — which also resolves, silently
        # reviewing a completely different range.
        if _ref_exists(repo, base):
            return base
        # Only now assume it is a bare branch name, as Actions passes ("main").
        if _ref_exists(repo, f"origin/{base}"):
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


def _git(args: list[str], repo: Path) -> subprocess.CompletedProcess:
    """Run git with list args (no shell) and never raise on a bad exit."""
    return subprocess.run(  # noqa: S603 - list args, no shell
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )


def _ref_exists(repo: Path, ref: str) -> bool:
    return _git(["rev-parse", "--verify", ref], repo).returncode == 0


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


def review_pr_url(
    url: str,
    local_repo: Path | None = None,
    base_override: str | None = None,
) -> str:
    """Review a GitHub PR by URL: fetch it, then review it like any range."""
    from codebase_rag.graph.pr import current_ref, parse_pr_url, prepare_pr

    pr = parse_pr_url(url)
    if pr is None:
        raise ValueError(f"not a GitHub PR URL: {url!r}")

    # Capture where the user's checkout was BEFORE anything moves it, so the
    # review can put it back. Read after prepare_pr and we would only see the
    # detached PR head.
    local = Path(local_repo).expanduser().resolve() if local_repo else None
    was_at = current_ref(local) if local and (local / ".git").exists() else None

    repo, base, head = prepare_pr(pr, local_repo=local_repo, base_override=base_override)
    # Only restore if we actually moved the user's tree, not a cached clone.
    restore_to = was_at if (local and repo == local) else None
    try:
        db = default_db_path(repo)
        build_graph(repo, db_path=db)
        report = format_report(review_range(repo, base=base, head=head, db_path=db))
    finally:
        if restore_to:
            _git(["checkout", "--quiet", restore_to], repo)
            logger.info(f"Restored {repo.name} to {restore_to}")
    return f"{pr.slug}\n{report}"


def build_report(repo: Path, base: str | None = None, head: str = "HEAD") -> str:
    """Graph-backed review: the same findings the UI and MCP tools produce.

    Errors propagate on purpose. An earlier version fell back to a regex
    reviewer on any exception, which silently swallowed real failures — a
    too-broad scope produced noisy findings instead of the error saying so.
    """
    repo = repo.resolve()
    db = default_db_path(repo)
    # Incremental: only re-parses changed files after the first build.
    build_graph(repo, db_path=db)
    return format_report(
        review_range(repo, base=_resolve_base(repo, base), head=head, db_path=db)
    )


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
        "--pr",
        default=_env("PRGRAF_PR"),
        help="Review a GitHub PR by URL (https://github.com/owner/repo/pull/123)",
    )
    parser.add_argument(
        "--uncommitted",
        action="store_true",
        help="Review working-tree changes against base (use before committing)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Only print the report (less log noise)",
    )
    args = parser.parse_args(argv)

    if args.quiet:
        logger.remove()

    if args.pr:
        try:
            report = review_pr_url(
                args.pr,
                local_repo=Path(args.repo) if args.repo else None,
                base_override=args.base,  # for a stacked PR based on another branch
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"PR review failed: {exc}")
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(report)
        if args.output:
            Path(args.output).write_text(
                f"## Blast-Radius PR Review\n\n```text\n{report}\n```\n", encoding="utf-8"
            )
        return 1 if _should_fail(report, args.fail_on) else 0

    repo = Path(args.repo)
    if not (repo / ".git").exists() and not (repo / ".git").is_file():
        # Allow worktrees / nested checkouts where .git may be a file
        if _git(["rev-parse", "--git-dir"], repo).returncode != 0:
            print(f"error: not a git repository: {repo}", file=sys.stderr)
            return 2

    try:
        head = WORKTREE if args.uncommitted else args.head
        # Uncommitted review compares against HEAD by default: "what am I about
        # to commit", not "what changed since main".
        base = args.base or ("HEAD" if args.uncommitted else None)
        report = build_report(repo=repo, base=base, head=head)
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
