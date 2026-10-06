"""Link pass, ref safety, and PR origin rules.

Each test pins a bug found by reviewing the engine: module/package calls that
never linked, incremental builds that disagreed with full builds, stale test
coverage, refs reaching git as options, and loose origin matching.

    pytest tests/test_link.py
    python tests/test_link.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from codebase_rag.graph import pr as pr_mod  # noqa: E402
from codebase_rag.graph.build import build_graph  # noqa: E402
from codebase_rag.graph.diff import UnsafeRefError  # noqa: E402
from codebase_rag.graph.review import review_range  # noqa: E402
from codebase_rag.graph.store import GraphStore, default_db_path  # noqa: E402
from test_pr import NUMBER, OWNER, REPO, _make_remote_and_clone  # noqa: E402


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def _tree(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="prgraf_link_"))
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root


def _callers(root: Path, qn: str) -> list[str]:
    with GraphStore(default_db_path(root)) as store:
        return [n.qualified_name for n in store.callers_of(qn)]


def test_go_call_through_a_package_import_links():
    root = _tree({
        "go.mod": "module github.com/org/proj\n",
        "auth/auth.go": "package auth\n\nfunc Verify(t string) bool {\n\treturn t != \"\"\n}\n",
        "cmd/main.go": (
            "package main\n\nimport (\n\t\"fmt\"\n\t\"github.com/org/proj/auth\"\n)\n\n"
            "func main() {\n\tfmt.Println(auth.Verify(\"x\"))\n}\n"
        ),
    })
    build_graph(root, full=True)
    assert _callers(root, "auth/auth.go::Verify") == ["cmd/main.go::main"]


def test_python_call_through_a_module_import_links():
    root = _tree({
        "billing.py": "def charge(x):\n    return x\n",
        "app.py": "import billing\n\n\ndef pay(x):\n    return billing.charge(x)\n",
        "pkg/__init__.py": "",
        "pkg/mail.py": "def send(x):\n    return x\n",
        "job.py": "from pkg import mail\n\n\ndef run():\n    mail.send(1)\n",
    })
    build_graph(root, full=True)
    assert _callers(root, "billing.py::charge") == ["app.py::pay"]
    assert _callers(root, "pkg/mail.py::send") == ["job.py::run"]


def test_a_function_added_later_links_to_unchanged_callers():
    root = _tree({"a.py": "def caller():\n    return helper_fn()\n"})
    build_graph(root)
    (root / "b.py").write_text("def helper_fn():\n    return 1\n", encoding="utf-8")
    build_graph(root)  # incremental: a.py is not re-parsed
    assert _callers(root, "b.py::helper_fn") == ["a.py::caller"]


def test_a_deleted_method_does_not_rebind_to_an_unrelated_function():
    root = _tree({
        "a.py": "class Job:\n    def process(self):\n        return 1\n",
        "b.py": "def run(job):\n    return job.process()\n",
        "c.py": "def process(x):\n    return x\n",
    })
    build_graph(root)
    (root / "a.py").write_text("class Job:\n    pass\n", encoding="utf-8")
    build_graph(root)  # b.py unchanged; its edge pointed at the removed method
    assert _callers(root, "c.py::process") == [], "receiver call rebound as a bare call"


def test_coverage_goes_when_the_last_test_stops_calling():
    root = _tree({
        "pay.py": "def pay(x):\n    return x\n",
        "tests/test_pay.py": "from pay import pay\n\n\ndef test_pay():\n    assert pay(1)\n",
    })
    build_graph(root)
    (root / "tests/test_pay.py").write_text("def test_pay():\n    assert True\n", encoding="utf-8")
    build_graph(root)
    with GraphStore(default_db_path(root)) as store:
        assert store.tests_for("pay.py::pay") == []


def test_option_shaped_refs_never_reach_git():
    root = _tree({"a.py": "def f():\n    return 1\n"})
    _git(["init", "-q"], root)
    out = root / "pwned.txt"
    build_graph(root)
    for base, head in ((f"HEAD", f"--output={out}"), ("-R", "HEAD"), ("--cached", "HEAD")):
        try:
            review_range(root, base=base, head=head)
        except UnsafeRefError:
            pass
        else:
            raise AssertionError(f"accepted {base!r}..{head!r}")
    assert not out.exists(), "git wrote a file named by the ref"


def test_origin_must_match_owner_and_repo_exactly():
    ref = pr_mod.PRRef("owner", "repo", 1)
    cases = {
        "git@github.com:owner/repo.git": True,
        "https://github.com/Owner/Repo/": True,
        "git@github.com:evilowner/repo-fork.git": False,
        "https://github.com/owner/repo-fork.git": False,
    }
    for url, expected in cases.items():
        with mock.patch.object(pr_mod, "_git", return_value=mock.Mock(stdout=url)):
            assert pr_mod._origin_matches(Path("."), ref) is expected, url


def test_a_reused_clone_compares_against_the_current_default_branch():
    root, clone = _make_remote_and_clone()
    pr = pr_mod.PRRef(OWNER, REPO, NUMBER)
    pr_mod.prepare_pr(pr, local_repo=clone)
    _git(["checkout", "-q", "main"], clone)

    # main moves on the remote after the clone last fetched.
    seed = root / "seed2"
    remote = root / OWNER / f"{REPO}.git"
    _git(["clone", "-q", str(remote), str(seed)], root)
    for key, value in (("user.email", "t@t.t"), ("user.name", "t")):
        _git(["config", key, value], seed)
    (seed / "other.py").write_text("def x():\n    return 2\n", encoding="utf-8")
    _git(["add", "-A"], seed)
    _git(["commit", "-qm", "main moves"], seed)
    _git(["push", "-q", "origin", "main"], seed)
    new_main = _git(["rev-parse", "HEAD"], seed).stdout.strip()

    _, base, _ = pr_mod.prepare_pr(pr, local_repo=clone)
    assert _git(["merge-base", "--is-ancestor", base, new_main], clone).returncode == 0
    assert _git(["rev-parse", "origin/main"], clone).stdout.strip() == new_main


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
