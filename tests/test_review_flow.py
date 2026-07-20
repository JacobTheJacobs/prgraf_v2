"""End-to-end review flow: the seams prgraf flagged as untested.

Covers `review_pr_url` (including that it puts a moved checkout back) and the
`web_payload` contract the web app, the HTML export, and the VS Code extension
all render from — if that shape drifts, every surface breaks at once.

    pytest tests/test_review_flow.py
    python tests/test_review_flow.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from codebase_rag.cli import review_pr_url  # noqa: E402
from codebase_rag.graph.build import build_graph  # noqa: E402
from codebase_rag.graph.pr import current_ref  # noqa: E402
from codebase_rag.graph.review import review_range, web_payload  # noqa: E402
from codebase_rag.graph.store import default_db_path  # noqa: E402
from test_pr import _make_remote_and_clone  # noqa: E402


def _git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )


def _repo_with_change() -> Path:
    """A repo whose last commit changes a function that has a caller."""
    root = Path(tempfile.mkdtemp(prefix="prgraf_flow_"))
    _git(["init", "-q", "-b", "main"], root)
    for key, value in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(["config", key, value], root)

    (root / "core.py").write_text(
        "def login(user):\n    return token(user)\n\n\ndef token(user):\n    return user\n",
        encoding="utf-8",
    )
    (root / "web.py").write_text(
        "from core import login\n\n\ndef handler(req):\n    return login(req)\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], root)
    _git(["commit", "-qm", "one"], root)

    (root / "core.py").write_text(
        "def login(user):\n    check(user)\n    return token(user)\n\n\n"
        "def check(user):\n    return bool(user)\n\n\ndef token(user):\n    return user\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], root)
    _git(["commit", "-qm", "two"], root)
    return root


def test_review_pr_url_restores_the_original_branch():
    """Reviewing checks the tree out at the PR head; it must be put back."""
    _root, clone = _make_remote_and_clone()
    before = current_ref(clone)
    report = review_pr_url(
        "https://github.com/testowner/testrepo/pull/7", local_repo=clone
    )
    assert "testowner/testrepo#7" in report, report
    assert current_ref(clone) == before, f"left on {current_ref(clone)}, was {before}"


def test_review_pr_url_rejects_a_non_pr_url():
    raised = False
    try:
        review_pr_url("https://example.com/nope")
    except ValueError:
        raised = True
    assert raised


def test_web_payload_shape_is_stable():
    """The contract every UI renders from."""
    repo = _repo_with_change()
    db = default_db_path(repo)
    build_graph(repo, db_path=db, full=True)
    result = review_range(repo, base="HEAD~1", head="HEAD", db_path=db)
    payload = web_payload(result, cap=50)

    for key in ("status", "report", "overall_risk", "findings", "changed_files", "graph"):
        assert key in payload, f"missing {key}"
    for key in ("seeds", "nodes", "edges", "impact_scores", "shown", "total"):
        assert key in payload["graph"], f"missing graph.{key}"
    for key in ("score", "level"):
        assert key in payload["overall_risk"], f"missing overall_risk.{key}"

    assert payload["findings"], "a changed, referenced symbol should be found"
    for key in ("symbol", "name", "severity", "risk", "location", "impacted"):
        assert key in payload["findings"][0], f"missing finding.{key}"


def test_render_cap_is_honoured():
    """The cap keeps the UI readable; nothing tested it, so it silently 10x'd."""
    repo = _repo_with_change()
    db = default_db_path(repo)
    build_graph(repo, db_path=db, full=True)
    result = review_range(repo, base="HEAD~1", head="HEAD", db_path=db)

    small = web_payload(result, cap=1)["graph"]
    seeds = len(small["seeds"])
    # Seeds are always kept; the cap bounds what is added on top of them.
    assert small["shown"] <= seeds + 1, f"cap ignored: {small['shown']} nodes"
    assert small["shown"] <= web_payload(result, cap=50)["graph"]["shown"]


def test_every_rendered_edge_has_both_endpoints():
    """A renderer silently drops edges whose endpoints are missing."""
    repo = _repo_with_change()
    db = default_db_path(repo)
    build_graph(repo, db_path=db, full=True)
    graph = web_payload(
        review_range(repo, base="HEAD~1", head="HEAD", db_path=db), cap=50
    )["graph"]
    ids = {n["qualified_name"] for n in graph["nodes"]}
    dangling = [e for e in graph["edges"] if e["source"] not in ids or e["target"] not in ids]
    assert not dangling, f"{len(dangling)} edges reference missing nodes"


def test_caller_is_found_across_files():
    """The whole point: a change must surface the callers it can break."""
    repo = _repo_with_change()
    db = default_db_path(repo)
    build_graph(repo, db_path=db, full=True)
    payload = web_payload(review_range(repo, base="HEAD~1", head="HEAD", db_path=db))
    names = {f["name"] for f in payload["findings"]}
    assert "login" in names, names
    reached = {
        qn.split("::")[-1]
        for f in payload["findings"]
        for qn in f["top_impacted"]
    }
    assert "handler" in reached, f"cross-file caller missing: {reached}"


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
