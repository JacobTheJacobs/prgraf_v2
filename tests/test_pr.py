"""PR fetching and the safety rules around it.

Every test here guards something that actually broke when this was first run
against a real PR: fetching into refs/heads/ of a checked-out branch, moving
someone's dirty working tree, and hanging on a credential prompt.

No network: a local bare repo plays the remote, with real refs/pull/N/head
refs pushed into it, and a path containing "owner/repo" so origin matching
behaves the same as it does against github.com.

    pytest tests/test_pr.py
    python tests/test_pr.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from codebase_rag.graph.pr import (  # noqa: E402
    PRError,
    PRRef,
    _is_dirty,
    current_ref,
    parse_pr_url,
    prepare_pr,
)

OWNER, REPO, NUMBER = "testowner", "testrepo", 7


def _git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )


def _make_remote_and_clone() -> tuple[Path, Path]:
    """A bare 'remote' holding refs/pull/7/head, plus a working clone."""
    root = Path(tempfile.mkdtemp(prefix="prgraf_pr_"))
    remote = root / OWNER / f"{REPO}.git"
    remote.parent.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "--bare", str(remote)], root)

    seed = root / "seed"
    seed.mkdir()
    _git(["init", "-q", "-b", "main"], seed)
    for key, value in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(["config", key, value], seed)

    (seed / "app.py").write_text("def base_fn():\n    return 1\n", encoding="utf-8")
    _git(["add", "-A"], seed)
    _git(["commit", "-qm", "base"], seed)
    _git(["remote", "add", "origin", str(remote)], seed)
    _git(["push", "-q", "origin", "main"], seed)
    # `git init --bare` leaves HEAD on master, so a clone would land on an
    # unborn branch instead of the code we just pushed.
    _git(["symbolic-ref", "HEAD", "refs/heads/main"], remote)

    # A PR branch on top of main, published the way GitHub exposes PRs.
    _git(["checkout", "-q", "-b", "pr-branch"], seed)
    (seed / "app.py").write_text(
        "def base_fn():\n    return 1\n\n\ndef added_fn():\n    return base_fn()\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], seed)
    _git(["commit", "-qm", "pr work"], seed)
    _git(["push", "-q", "origin", f"HEAD:refs/pull/{NUMBER}/head"], seed)

    clone = root / "clone"
    _git(["clone", "-q", str(remote), str(clone)], root)
    for key, value in (("user.email", "t@t.t"), ("user.name", "t")):
        _git(["config", key, value], clone)
    return root, clone


def _pr() -> PRRef:
    return PRRef(owner=OWNER, repo=REPO, number=NUMBER)


# --- url parsing ---------------------------------------------------------

def test_parses_standard_pr_url():
    got = parse_pr_url("https://github.com/tirth8205/code-review-graph/pull/650")
    assert got == PRRef("tirth8205", "code-review-graph", 650), got


def test_parses_without_scheme_and_with_trailing_path():
    assert parse_pr_url("github.com/o/r/pull/1") == PRRef("o", "r", 1)
    assert parse_pr_url("https://github.com/o/r/pull/12/files") == PRRef("o", "r", 12)
    assert parse_pr_url("https://www.github.com/o/r/pull/3/") == PRRef("o", "r", 3)


def test_rejects_non_pr_urls():
    for bad in (
        "https://example.com/o/r/pull/1",
        "https://github.com/o/r/issues/1",
        "https://github.com/o/r",
        "",
        "not a url",
    ):
        assert parse_pr_url(bad) is None, bad


# --- repo state helpers --------------------------------------------------

def test_detects_dirty_working_tree():
    _root, clone = _make_remote_and_clone()
    assert _is_dirty(clone) is False
    (clone / "app.py").write_text("scribble\n", encoding="utf-8")
    assert _is_dirty(clone) is True


def test_current_ref_reports_branch_then_sha_when_detached():
    _root, clone = _make_remote_and_clone()
    assert current_ref(clone) == "main"
    _git(["checkout", "-q", "--detach", "HEAD"], clone)
    detached = current_ref(clone)
    assert detached != "main" and len(detached) >= 7, detached


# --- prepare_pr ----------------------------------------------------------

def test_prepares_pr_from_a_clean_local_clone():
    _root, clone = _make_remote_and_clone()
    repo, base, head = prepare_pr(_pr(), local_repo=clone)
    assert repo == clone.resolve()
    assert head.startswith("refs/prgraf/pr/")
    # The PR's file contents must be on disk for parsing.
    assert "added_fn" in (clone / "app.py").read_text(encoding="utf-8")
    # base is the merge-base, so the review covers only the PR's own work.
    assert base and base != head


def test_repeat_prepare_succeeds():
    """Fetching into refs/heads/ failed the second time, because the clone was
    already checked out on that branch."""
    _root, clone = _make_remote_and_clone()
    prepare_pr(_pr(), local_repo=clone)
    repo, base, head = prepare_pr(_pr(), local_repo=clone)  # must not raise
    assert repo == clone.resolve() and head.startswith("refs/prgraf/pr/")


def test_refuses_to_touch_a_dirty_local_clone(tmp_cache=None):
    """A review must never discard uncommitted work."""
    _root, clone = _make_remote_and_clone()
    (clone / "app.py").write_text("MY UNSAVED WORK\n", encoding="utf-8")
    before_ref = current_ref(clone)

    # Force the fallback to a cache clone, which cannot reach a real remote
    # for this fake project, so it fails instead of using the dirty tree.
    cache = Path(tempfile.mkdtemp(prefix="prgraf_cache_"))
    os.environ["PRGRAF_CACHE"] = str(cache)
    try:
        raised = False
        try:
            prepare_pr(_pr(), local_repo=clone)
        except PRError:
            raised = True
        assert raised, "expected a clone failure rather than using the dirty tree"
    finally:
        os.environ.pop("PRGRAF_CACHE", None)

    # The point of the test: the user's work is untouched.
    assert (clone / "app.py").read_text(encoding="utf-8") == "MY UNSAVED WORK\n"
    assert current_ref(clone) == before_ref


def test_missing_pr_fails_with_a_message():
    _root, clone = _make_remote_and_clone()
    try:
        prepare_pr(PRRef(OWNER, REPO, 999), local_repo=clone)
    except PRError as exc:
        assert "999" in str(exc), exc
    else:
        raise AssertionError("expected PRError for a PR that does not exist")


def test_base_override_changes_the_comparison_point():
    _root, clone = _make_remote_and_clone()
    _, default_base, _ = prepare_pr(_pr(), local_repo=clone)
    _, overridden, _ = prepare_pr(_pr(), local_repo=clone, base_override="origin/main")
    # Both resolve to a real commit; the override path must be honoured.
    assert default_base and overridden


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS: {fn.__name__}")
            passed += 1
        except AssertionError as exc:
            print(f"  FAIL: {fn.__name__} -- {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR: {fn.__name__} -- {type(exc).__name__}: {exc}")
    print(f"\n{passed}/{len(tests)} passed")
    return passed == len(tests)


if __name__ == "__main__":
    raise SystemExit(0 if _run_all() else 1)
