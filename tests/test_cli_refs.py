"""Base-ref resolution.

Guards a bug that silently reviewed the wrong range: `_resolve_base` prefixed
"origin/" onto anything that was not already qualified, so "HEAD~1" became
"origin/HEAD~1" — which also resolves in a repo with an origin, so nothing
failed. The review just quietly described a different diff.

Runnable two ways:
    pytest tests/test_cli_refs.py
    python tests/test_cli_refs.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from codebase_rag.cli import _resolve_base  # noqa: E402


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo_with_origin() -> Path:
    """A repo whose 'origin' remote resolves, mirroring CI and local clones."""
    remote = Path(tempfile.mkdtemp(prefix="prgraf_remote_"))
    _git(["init", "-q", "--bare"], remote)

    work = Path(tempfile.mkdtemp(prefix="prgraf_work_"))
    _git(["init", "-q"], work)
    _git(["config", "user.email", "t@t.t"], work)
    _git(["config", "user.name", "t"], work)
    _git(["config", "commit.gpgsign", "false"], work)
    (work / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    _git(["add", "-A"], work)
    _git(["commit", "-qm", "one"], work)
    (work / "a.py").write_text("def a():\n    return 2\n", encoding="utf-8")
    _git(["add", "-A"], work)
    _git(["commit", "-qm", "two"], work)
    _git(["remote", "add", "origin", str(remote)], work)
    _git(["push", "-q", "origin", "HEAD:refs/heads/main"], work)
    _git(["fetch", "-q", "origin"], work)
    return work


def test_revision_syntax_is_not_prefixed_with_origin():
    repo = _repo_with_origin()
    for ref in ("HEAD~1", "HEAD~2", "HEAD"):
        assert _resolve_base(repo, ref) == ref, f"{ref} was rewritten"


def test_bare_branch_name_resolves_to_origin():
    repo = _repo_with_origin()
    # "main" exists only on the remote here, as a PR base does in CI.
    assert _resolve_base(repo, "main") == "origin/main"


def test_already_qualified_ref_is_left_alone():
    repo = _repo_with_origin()
    assert _resolve_base(repo, "origin/main") == "origin/main"
    assert _resolve_base(repo, "refs/heads/main") == "refs/heads/main"


def test_sha_is_left_alone():
    repo = _repo_with_origin()
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert _resolve_base(repo, sha) == sha


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
