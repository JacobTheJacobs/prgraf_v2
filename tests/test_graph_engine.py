"""Engine tests: extraction, linking, impact, risk, diff seeding.

These lock the load-bearing behavior that was wrong at least once during
development: symbol-level (not file-level) seeding, File-node reach damping,
the strong/weak security-term split, and TESTED_BY direction.

Runnable two ways:
    pytest tests/test_graph_engine.py
    python tests/test_graph_engine.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from codebase_rag.graph.build import build_graph  # noqa: E402
from codebase_rag.graph.diff import map_ranges_to_nodes, parse_diff_ranges  # noqa: E402
from codebase_rag.graph.risk import (  # noqa: E402
    _security_component,
    risk_level,
    score_symbol,
)
from codebase_rag.graph.store import GraphNode, GraphStore  # noqa: E402

# --- a tiny synthetic repo the graph should understand -------------------

FILES = {
    "auth/session.py": (
        "SECRET = 'x'\n"
        "\n"
        "def login(user, pw):\n"
        "    return make_token(user)\n"
        "\n"
        "def make_token(user):\n"
        "    return hash_pw(user)\n"
        "\n"
        "def hash_pw(user):\n"
        "    return user\n"
    ),
    "app/handlers.py": (
        "from auth.session import login\n"
        "\n"
        "def handle_request(req):\n"
        "    return login(req.user, req.pw)\n"
        "\n"
        "def unrelated_helper():\n"
        "    return 1\n"
    ),
    "tests/test_session.py": (
        "from auth.session import login\n"
        "\n"
        "def test_login():\n"
        "    assert login('a', 'b')\n"
    ),
}


def _make_repo() -> Path:
    root = Path(tempfile.mkdtemp(prefix="prgraf_test_"))
    for rel, content in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def _store(root: Path) -> GraphStore:
    db = root / ".prgraf" / "graph.db"
    build_graph(root, db_path=db, full=True)
    return GraphStore(db)


# --- tests ---------------------------------------------------------------

def test_extraction_finds_symbols():
    root = _make_repo()
    store = _store(root)
    login = store.get_node("auth/session.py::login")
    assert login is not None, "login should be extracted as a symbol"
    assert login.kind == "Function"


def test_calls_are_resolved_cross_file():
    root = _make_repo()
    store = _store(root)
    # handle_request calls login; the resolver must qualify the bare "login"
    callers = store.callers_of("auth/session.py::login")
    caller_names = {c.qualified_name for c in callers}
    assert "app/handlers.py::handle_request" in caller_names, caller_names


def test_tested_by_direction():
    # TESTED_BY is production -> test; getting it backwards reports zero coverage
    root = _make_repo()
    store = _store(root)
    tests = store.tests_for("auth/session.py::login")
    names = {t.qualified_name for t in tests}
    assert any("test_session.py" in n for n in names), names


def test_impact_is_bidirectional_and_ranked():
    root = _make_repo()
    store = _store(root)
    impact = store.get_impact_radius(
        seed_qualified_names={"auth/session.py::login"}
    )
    scores = impact["impact_scores"]
    # reaches its caller (handle_request) and its callee (make_token)
    assert "app/handlers.py::handle_request" in scores
    assert "auth/session.py::make_token" in scores
    # direct neighbors score higher than the seed's second hop
    assert scores["auth/session.py::make_token"] >= scores.get(
        "auth/session.py::hash_pw", 0
    )


def test_impact_decays_with_distance():
    root = _make_repo()
    store = _store(root)
    impact = store.get_impact_radius(
        seed_qualified_names={"auth/session.py::login"}, max_depth=3
    )
    scores = impact["impact_scores"]
    # make_token is one hop, hash_pw is two hops from login
    assert scores["auth/session.py::make_token"] > scores["auth/session.py::hash_pw"]


def test_diff_seeds_at_symbol_level():
    root = _make_repo()
    store = _store(root)
    # a hunk touching only login's body (lines 3-4)
    ranges = {"auth/session.py": [(3, 4)]}
    seeds = map_ranges_to_nodes(store, ranges)
    names = {s.qualified_name for s in seeds}
    assert names == {"auth/session.py::login"}, names
    # crucially NOT the whole file, and not the other functions
    assert "auth/session.py::hash_pw" not in names


def test_receiver_call_does_not_bind_to_an_unrelated_function():
    """`x.add(...)` must not resolve to some top-level `add` elsewhere.

    An agent reviewing a real PR reported a webview->host call edge that did
    not exist: `classList.add(...)` had bound to a `const add = ...` closure in
    another file purely on the name.
    """
    root = _make_repo()
    (root / "ui.py").write_text(
        "def paint(el):\n"
        "    el.classList.add('on')\n",
        encoding="utf-8",
    )
    (root / "helpers.py").write_text(
        "def wrapper():\n"
        "    def add(raw):\n"
        "        return raw\n"
        "    return add\n",
        encoding="utf-8",
    )
    store = _store(root)
    targets = {
        e.target_qualified
        for e in store.edges_between({
            n.qualified_name for n in store.find_nodes("paint", limit=5)
        } | {n.qualified_name for n in store.find_nodes("add", limit=5)})
    }
    assert not any("helpers.py" in t for t in targets), targets


def test_receiver_call_still_binds_to_a_real_method():
    """The strict rule must not sever genuine cross-file method calls."""
    root = _make_repo()
    (root / "svc.py").write_text(
        "class Store:\n"
        "    def fetch(self):\n"
        "        return 1\n",
        encoding="utf-8",
    )
    (root / "use.py").write_text(
        "from svc import Store\n"
        "\n"
        "def run(store):\n"
        "    return store.fetch()\n",
        encoding="utf-8",
    )
    store = _store(root)
    callers = {c.name for c in store.callers_of("svc.py::Store.fetch")}
    assert "run" in callers, callers


def test_diff_seeds_innermost_symbol_not_enclosing_class():
    """A change inside a method must not also seed its class.

    The class spans every method, so its reach is far larger; seeding both
    lets the class outrank the method that actually changed.
    """
    root = _make_repo()
    (root / "app" / "svc.py").write_text(
        "class Service:\n"
        "    def alpha(self):\n"
        "        return 1\n"
        "\n"
        "    def beta(self):\n"
        "        return 2\n",
        encoding="utf-8",
    )
    store = _store(root)
    seeds = map_ranges_to_nodes(store, {"app/svc.py": [(3, 3)]})  # inside alpha
    names = {s.name for s in seeds}
    assert names == {"alpha"}, names
    assert "Service" not in names


def test_diff_falls_back_to_file_for_module_level_edit():
    root = _make_repo()
    store = _store(root)
    # line 1 is the module-level SECRET assignment, inside no function
    seeds = map_ranges_to_nodes(store, {"auth/session.py": [(1, 1)]})
    kinds = {s.kind for s in seeds}
    assert "File" in kinds, "module-level edit should seed the File node"


def test_parse_diff_ranges_handles_deletions():
    diff = (
        "+++ b/foo.py\n"
        "@@ -10,3 +10,0 @@\n"   # pure deletion: count 0 on the new side
        "+++ b/bar.py\n"
        "@@ -1,0 +5,2 @@\n"
    )
    ranges = parse_diff_ranges(diff)
    assert ranges["foo.py"] == [(10, 10)]  # anchored, not skipped
    assert ranges["bar.py"] == [(5, 6)]


def test_security_term_split():
    def n(name, path):
        return GraphNode(kind="Function", name=name, qualified_name=f"{path}::{name}",
                         file_path=path)

    # strong term in the name alone => full weight
    assert _security_component(n("login", "x/y.py")) == 0.20
    # weak generic term, neutral path => low weight (the upstream noise fix)
    assert _security_component(n("validate_input", "utils/helpers.py")) == 0.05
    # same weak term on a sensitive path => promoted above the neutral floor
    # (here via the strong-term-in-path branch, since "auth" is in the path)
    assert _security_component(n("validate_input", "auth/session.py")) >= 0.15
    # weak term corroborated by a sensitive dir that is not itself a strong term
    assert _security_component(n("run_query", "db/models.py")) == 0.20


def test_untested_symbol_scores_higher_than_tested():
    root = _make_repo()
    store = _store(root)
    login = store.get_node("auth/session.py::login")          # has a test
    helper = store.get_node("app/handlers.py::unrelated_helper")  # has none
    imp_login = store.get_impact_radius(seed_qualified_names={login.qualified_name})
    imp_helper = store.get_impact_radius(seed_qualified_names={helper.qualified_name})
    r_login = score_symbol(store, login, imp_login)
    r_helper = score_symbol(store, helper, imp_helper)
    assert r_helper.factors.untested > r_login.factors.untested


def test_risk_level_thresholds():
    assert risk_level(0.9) == "critical"
    assert risk_level(0.72) == "high"
    assert risk_level(0.5) == "medium"
    assert risk_level(0.2) == "low"


# --- runner --------------------------------------------------------------

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
