"""Review a GitHub pull request by URL.

Turns `https://github.com/owner/repo/pull/123` into a local checkout plus the
`base...head` range the rest of the pipeline already understands. A PR is
reviewed exactly like any other range once it is on disk — there is no
separate PR code path.

Clones are cached per repo so the second review of a project is fast.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from .constants import DB_DIRNAME

PR_URL_RE = re.compile(
    # Scheme optional: people paste "github.com/owner/repo/pull/1" just as often.
    r"^(?:https?://)?(?:www\.)?github\.com/"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)"
    r"/pull/(?P<number>\d+)/?",
    re.IGNORECASE,
)

GIT_TIMEOUT = 600


class PRError(RuntimeError):
    """A PR could not be prepared for review."""


@dataclass(frozen=True)
class PRRef:
    owner: str
    repo: str
    number: int

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"

    @property
    def clone_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}.git"


def parse_pr_url(url: str) -> PRRef | None:
    """Parse a GitHub PR URL. Returns None if it is not one."""
    match = PR_URL_RE.match((url or "").strip())
    if not match:
        return None
    return PRRef(
        owner=match.group("owner"),
        repo=match.group("repo").removesuffix(".git"),
        number=int(match.group("number")),
    )


def looks_like_pr_url(value: str) -> bool:
    return parse_pr_url(value) is not None


def _git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    import os

    # Never let git block on a credential prompt: a private repo would
    # otherwise hang until the timeout instead of saying it needs access.
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "GCM_INTERACTIVE": "never",
    }
    return subprocess.run(  # noqa: S603 - list args, no shell
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        env=env,
        timeout=GIT_TIMEOUT,
        check=False,
    )


def cache_root() -> Path:
    import os

    override = os.environ.get("PRGRAF_CACHE")
    root = Path(override) if override else Path.home() / f"{DB_DIRNAME}-cache"
    return root / "repos"


def _origin_matches(repo_path: Path, pr: PRRef) -> bool:
    """Is this checkout a clone of the PR's project?

    Normalises separators before comparing: an origin can be an SSH URL, an
    HTTPS URL, or a filesystem path, and on Windows that path uses
    backslashes — which a naive substring check never matches.
    """
    url = _git(["remote", "get-url", "origin"], repo_path).stdout.strip().lower()
    if not url:
        return False
    normalized = url.replace("\\", "/").replace(":", "/").removesuffix(".git")
    return f"{pr.owner}/{pr.repo}".lower() in normalized


def _is_dirty(repo_path: Path) -> bool:
    return bool(_git(["status", "--porcelain"], repo_path).stdout.strip())


def current_ref(repo_path: Path) -> str:
    """Branch name, or a commit sha when detached — enough to restore later."""
    branch = _git(["symbolic-ref", "--quiet", "--short", "HEAD"], repo_path).stdout.strip()
    return branch or _git(["rev-parse", "HEAD"], repo_path).stdout.strip()


def prepare_pr(
    pr: PRRef,
    local_repo: Path | None = None,
    base_override: str | None = None,
) -> tuple[Path, str, str]:
    """Fetch a PR and return (repo_path, base, head).

    Uses `local_repo` only when it is a clone of the same project AND has no
    uncommitted changes — reviewing checks the tree out at the PR head, and
    silently discarding someone's work in progress is never acceptable. A
    cached clone is used otherwise.

    `base` is the merge-base with the default branch, so the review reflects
    only what the PR changed. Pass `base_override` for a stacked PR whose real
    base is another branch rather than the default one.
    """
    repo_path: Path | None = None

    if local_repo:
        candidate = Path(local_repo).expanduser().resolve()
        if (candidate / ".git").exists() and _origin_matches(candidate, pr):
            if _is_dirty(candidate):
                logger.warning(
                    f"{candidate} has uncommitted changes; using a cached clone "
                    "instead of checking it out."
                )
            else:
                repo_path = candidate
                logger.info(f"Using local clone for {pr.slug}: {repo_path}")

    if repo_path is None:
        target = cache_root() / f"{pr.owner}__{pr.repo}"
        if (target / ".git").exists():
            logger.info(f"Updating cached clone for {pr.slug}")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            logger.info(f"Cloning {pr.clone_url}")
            result = _git(["clone", "--quiet", pr.clone_url, str(target)])
            if result.returncode != 0:
                raise PRError(
                    f"could not clone {pr.clone_url}: {result.stderr.strip() or 'unknown error'}"
                )
        repo_path = target

    # Fetch into a custom namespace, not refs/heads/: on a repeat review the
    # cached clone is already checked out on the PR, and git refuses to fetch
    # into the current branch of a non-bare repo.
    head_ref = f"refs/prgraf/pr/{pr.number}"
    fetched = _git(
        ["fetch", "--quiet", "origin", f"refs/pull/{pr.number}/head:{head_ref}", "--force"],
        repo_path,
    )
    if fetched.returncode != 0:
        raise PRError(
            f"could not fetch {pr.slug}: {fetched.stderr.strip() or 'is it private, or does it exist?'}"
        )

    # A stacked PR's real base is another branch, which only the GitHub API
    # knows; assuming the default branch would fold the parent PR's changes
    # into this review. base_override exists for that case.
    reference = base_override or _default_branch(repo_path)
    merge_base = _git(["merge-base", reference, head_ref], repo_path).stdout.strip()
    base = merge_base or reference

    # Parsing needs the PR's file contents on disk, not just its refs.
    # Detached so the ref never becomes the checked-out branch.
    checkout = _git(["checkout", "--quiet", "--force", "--detach", head_ref], repo_path)
    if checkout.returncode != 0:
        raise PRError(f"could not check out {pr.slug}: {checkout.stderr.strip()}")

    logger.info(f"Prepared {pr.slug}: {base[:8]}..{head_ref}")
    return repo_path, base, head_ref


def _default_branch(repo_path: Path) -> str:
    head = _git(["symbolic-ref", "refs/remotes/origin/HEAD"], repo_path).stdout.strip()
    if head:
        return head.rsplit("/", 1)[-1] and f"origin/{head.rsplit('/', 1)[-1]}"
    for candidate in ("origin/main", "origin/master"):
        if _git(["rev-parse", "--verify", candidate], repo_path).returncode == 0:
            return candidate
    return "HEAD~1"
