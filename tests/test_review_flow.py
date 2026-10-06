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
from codebase_rag.graph.review import (  # noqa: E402
    EmptyGraphError,
    format_report,
    review_range,
    web_payload,
)
from codebase_rag.graph.store import GraphStore, default_db_path  # noqa: E402
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


def _repo_with_repeated_calls() -> Path:
    """One caller, many call sites — the shape that inflated caller counts."""
    root = Path(tempfile.mkdtemp(prefix="prgraf_dup_"))
    _git(["init", "-q", "-b", "main"], root)
    for key, value in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(["config", key, value], root)

    (root / "core.py").write_text("def helper(x):\n    return x\n", encoding="utf-8")
    (root / "caller.py").write_text(
        "from core import helper\n\n\ndef only_caller(a):\n"
        + "".join(f"    helper({i})\n" for i in range(8))
        + "    return helper(a)\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], root)
    _git(["commit", "-qm", "one"], root)
    (root / "core.py").write_text(
        "def helper(x):\n    y = x + 1\n    return y\n", encoding="utf-8"
    )
    _git(["add", "-A"], root)
    _git(["commit", "-qm", "two"], root)
    return root


def test_callers_counts_dependents_not_call_sites():
    """Nine calls from one function is one dependent, not nine.

    Raw edge rows made a symbol called in a loop read as widely depended on,
    which fed the "many callers" risk term. Real graphs ran 60% duplicates.
    """
    repo = _repo_with_repeated_calls()
    db = default_db_path(repo)
    build_graph(repo, db_path=db, full=True)

    with GraphStore(db) as store:
        target = next(
            n for n in store.find_nodes("helper", limit=10) if n.kind != "File"
        )
        rows = store._conn.execute(  # noqa: SLF001
            "SELECT COUNT(*) c FROM edges WHERE target_qualified = ? AND kind = 'CALLS'",
            (target.qualified_name,),
        ).fetchone()["c"]
        callers = store.callers_of(target.qualified_name)

    assert rows > len(callers), f"fixture did not repeat calls (rows={rows})"
    names = [c.qualified_name for c in callers]
    assert len(names) == len(set(names)), f"duplicate callers: {names}"


def test_an_empty_graph_raises_instead_of_reading_as_clean():
    """The worst failure this tool can have: silence that looks like a pass.

    An empty graph yields zero changed symbols, which used to render as
    "no reviewable symbols changed" — identical to a genuinely clean review.
    """
    repo = _repo_with_change()
    db = default_db_path(repo)
    GraphStore(db).close()  # creates the schema, inserts nothing

    raised = None
    try:
        review_range(repo, base="HEAD~1", head="HEAD", db_path=db)
    except EmptyGraphError as exc:
        raised = exc
    assert raised is not None, "an empty graph reported a clean review"
    assert str(repo) in str(raised), "the error must name the root it used"


def test_a_stale_graph_warns_at_minimal_detail():
    """Agents stop at minimal; that is exactly where the warning must appear."""
    repo = _repo_with_change()
    db = default_db_path(repo)
    build_graph(repo, db_path=db, full=True)

    with GraphStore(db) as store:
        store.set_meta("last_build", "2000-01-01T00:00:00")

    result = review_range(repo, base="HEAD~1", head="HEAD", db_path=db)
    assert result.stale, "commit is newer than the build; staleness missed"
    packet = result.to_dict(detail="minimal")
    assert "warning" in packet, f"minimal packet hides staleness: {packet.keys()}"
    assert "rebuild" in packet["warning"].lower()
    assert "rebuild" in format_report(result).lower()


def test_a_removed_function_that_is_still_called_is_a_finding():
    """Deleting code is the surest way to break a caller; it must surface."""
    repo = _repo_with_change()
    (repo / "util.py").write_text("def slugify(s):\n    return s\n", encoding="utf-8")
    (repo / "api.py").write_text(
        "from util import slugify\nfrom core import token\n\n\n"
        "def route(req):\n    return slugify(token(req))\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], repo)
    _git(["commit", "-qm", "add callers"], repo)

    # Remove `token` from core.py and all of util.py; api.py still calls both.
    (repo / "core.py").write_text(
        "def login(user):\n    check(user)\n    return user\n\n\n"
        "def check(user):\n    return bool(user)\n",
        encoding="utf-8",
    )
    _git(["rm", "-q", "util.py"], repo)
    _git(["add", "-A"], repo)
    _git(["commit", "-qm", "remove"], repo)

    db = default_db_path(repo)
    build_graph(repo, db_path=db)
    result = review_range(repo, base="HEAD~1", head="HEAD", db_path=db, max_findings=5)
    removed = {f.node.name: f for f in result.all_risks if f.node.extra.get("deleted")}
    assert set(removed) == {"token", "slugify"}, removed
    assert all(f.severity in ("P0", "P1") for f in removed.values()), removed
    assert "api.py::route" in removed["slugify"].top_impacted
    report = format_report(result)
    assert "was removed but 1 caller(s) still reference it" in report, report
    payload = web_payload(result)
    assert any(n.get("deleted") for n in payload["graph"]["nodes"]), "ghost node not rendered"


def test_a_removed_function_with_no_callers_is_not_a_finding():
    repo = _repo_with_change()
    (repo / "core.py").write_text(
        "def login(user):\n    return token(user)\n\n\ndef token(user):\n    return user\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], repo)
    _git(["commit", "-qm", "drop check"], repo)
    db = default_db_path(repo)
    build_graph(repo, db_path=db)
    result = review_range(repo, base="HEAD~1", head="HEAD", db_path=db)
    assert not [f for f in result.all_risks if f.node.extra.get("deleted")]


def test_a_utc_build_is_not_stale_against_an_east_of_utc_commit():
    """CI builds in UTC; a +03:00 commit made minutes earlier is not newer."""
    import os
    import time

    repo = _repo_with_change()
    env_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "UTC"
        time.tzset()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime(time.time() - 120))
        _git(["-c", "user.email=t@t.t", "commit", "-q", "--amend", "--no-edit",
              "--date", stamp], repo)
        env = {**os.environ, "GIT_COMMITTER_DATE": time.strftime(
            "%Y-%m-%dT%H:%M:%S+03:00", time.gmtime(time.time() - 120 + 3 * 3600))}
        subprocess.run(["git", "commit", "-q", "--amend", "--no-edit"], cwd=repo, env=env, check=True)
        db = default_db_path(repo)
        build_graph(repo, db_path=db)
        result = review_range(repo, base="HEAD~1", head="HEAD", db_path=db)
        assert not result.stale, result.stale
    finally:
        if env_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = env_tz
        time.tzset()


def test_uncommitted_edits_to_reviewed_files_are_flagged():
    repo = _repo_with_change()
    with open(repo / "core.py", "a", encoding="utf-8") as fh:
        fh.write("\n# local edit\n")
    db = default_db_path(repo)
    build_graph(repo, db_path=db)
    result = review_range(repo, base="HEAD~1", head="HEAD", db_path=db)
    assert result.stale and "uncommitted" in result.stale["message"], result.stale
    assert "uncommitted" in format_report(result)


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
