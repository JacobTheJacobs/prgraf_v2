"""Extraction tests: receivers, Go method scoping, nested qualified names,
TS overloads, and test-path detection.

Runnable two ways:
    pytest tests/test_extract.py
    python tests/test_extract.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from codebase_rag.graph.extract import (  # noqa: E402
    RECEIVER_CALL_CONFIDENCE,
    _is_test_path,
    extract_file,
)


def _extract(path: str, src: str, language: str):
    return extract_file(path, src.encode(), language)


def _calls(edges):
    return {e.target_qualified: e for e in edges if e.kind == "CALLS"}


def _by_qn(nodes):
    return {n.qualified_name: n for n in nodes}


# --- 1. receiver calls ----------------------------------------------------


def test_python_receiver_is_plain_identifier():
    src = (
        "import auth.session as sess\n"
        "from a import b\n"
        "class K:\n"
        "    def m(self):\n"
        "        sess.login()\n"
        "        self.helper()\n"
        "        self.x.chained()\n"
        "        make_thing()\n"
    )
    _, edges = _extract("m.py", src, "python")
    calls = _calls(edges)
    assert calls["login"].receiver == "sess"
    assert calls["login"].confidence == RECEIVER_CALL_CONFIDENCE
    assert calls["helper"].receiver is None
    assert calls["helper"].confidence == RECEIVER_CALL_CONFIDENCE
    assert calls["chained"].receiver is None
    assert calls["make_thing"].receiver is None
    assert calls["make_thing"].confidence == 0.5
    imports = {e.target_qualified for e in edges if e.kind == "IMPORTS_FROM"}
    assert imports == {"auth.session", "a"}, imports


def test_js_receiver_member_expression():
    src = (
        "import api from './api';\n"
        "function f() {\n"
        "  api.fetchUser();\n"
        "  this.render();\n"
        "  a.b.deepCall();\n"
        "  getIt().after();\n"
        "}\n"
    )
    _, edges = _extract("f.ts", src, "typescript")
    calls = _calls(edges)
    assert calls["fetchUser"].receiver == "api"
    assert calls["render"].receiver is None
    assert calls["deepCall"].receiver is None
    assert calls["after"].receiver is None


def test_go_receiver_and_import_paths():
    src = (
        "package x\n"
        "import (\n"
        '\tau "github.com/org/proj/auth"\n'
        '\t"fmt"\n'
        ")\n"
        'import "strings"\n'
        "func F() {\n"
        "\tau.Login()\n"
        "\tfmt.Println()\n"
        "}\n"
    )
    _, edges = _extract("x.go", src, "go")
    calls = _calls(edges)
    assert calls["Login"].receiver == "au"
    assert calls["Login"].confidence == RECEIVER_CALL_CONFIDENCE
    assert calls["Println"].receiver == "fmt"
    imports = {e.target_qualified for e in edges if e.kind == "IMPORTS_FROM"}
    assert imports == {"github.com/org/proj/auth", "fmt", "strings"}, imports


# --- 2. Go methods scoped by receiver type --------------------------------


def test_go_methods_scoped_by_receiver_type():
    src = (
        "package x\n"
        "type A struct{}\n"
        "type B[T any] struct{}\n"
        "func (a *A) Close() {}\n"
        "func (b *B[T]) Close() {}\n"
        "func (b B[T]) Open() {}\n"
        "func Close() {}\n"
    )
    nodes, _ = _extract("x.go", src, "go")
    qns = _by_qn(nodes)
    assert qns["x.go::A.Close"].parent_name == "A"
    assert qns["x.go::B.Close"].parent_name == "B"
    assert qns["x.go::B.Open"].parent_name == "B"
    assert qns["x.go::Close"].parent_name is None
    assert qns["x.go::A.Close"].line_start == 4
    assert qns["x.go::B.Close"].line_start == 5


# --- 3. nested names use the full in-file path ----------------------------


def test_nested_names_fully_qualified():
    src = (
        "def top():\n"
        "    pass\n"
        "class A:\n"
        "    def run(self):\n"
        "        def inner():\n"
        "            pass\n"
        "class B:\n"
        "    def run(self):\n"
        "        def inner():\n"
        "            pass\n"
    )
    nodes, edges = _extract("m.py", src, "python")
    qns = _by_qn(nodes)
    assert qns["m.py::top"].parent_name is None
    assert qns["m.py::A.run"].parent_name == "A"
    assert qns["m.py::A.run.inner"].parent_name == "A.run"
    assert qns["m.py::B.run.inner"].parent_name == "B.run"
    assert "m.py::run.inner" not in qns
    # parent_name round-trips to the parent's qualified name.
    for n in nodes:
        if n.parent_name:
            assert f"m.py::{n.parent_name}" in qns, n.qualified_name
    contains = {(e.source_qualified, e.target_qualified) for e in edges if e.kind == "CONTAINS"}
    assert ("m.py::A.run", "m.py::A.run.inner") in contains


# --- 4. TS overloads ------------------------------------------------------


def test_ts_overload_implementation_wins():
    src = (
        "function f(a: string): void;\n"
        "function f(a: number): void;\n"
        "function f(a: any) {\n"
        "  helperCall();\n"
        "}\n"
        "class C {\n"
        "  m(): void;\n"
        "  m(x?: any) {\n"
        "    other();\n"
        "  }\n"
        "}\n"
        "declare function onlySig(): void;\n"
    )
    nodes, edges = _extract("o.ts", src, "typescript")
    qns = _by_qn(nodes)
    assert [n.qualified_name for n in nodes].count("o.ts::f") == 1
    f = qns["o.ts::f"]
    assert (f.line_start, f.line_end) == (3, 5), (f.line_start, f.line_end)
    m = qns["o.ts::C.m"]
    assert (m.line_start, m.line_end) == (8, 10), (m.line_start, m.line_end)
    assert "o.ts::onlySig" in qns
    calls = {(e.source_qualified, e.target_qualified) for e in edges if e.kind == "CALLS"}
    assert ("o.ts::f", "helperCall") in calls
    assert ("o.ts::C.m", "other") in calls


# --- 5. test-path detection -----------------------------------------------


def test_test_path_detection():
    for p in (
        "tests/test_x.py", "pkg/test/util.py", "web/__tests__/a.js", "spec/models/user.rb",
        "test_foo.py", "foo_test.py", "foo_test.go", "a.test.ts", "a.test.tsx",
        "a.test.jsx", "a.spec.ts", "a.spec.js", "conftest.py", "src/tests/conftest.py",
    ):
        assert _is_test_path(p), p
    for p in (
        "inspector/run.py", "latest/x.py", "protest/a.go", "respect.py", "src/attest.ts",
        "contest.py", "specific/x.py", "lib/testing_utils.py",
    ):
        assert not _is_test_path(p), p


def test_inspector_functions_not_marked_test():
    nodes, _ = _extract("inspector/core.py", "def test_like():\n    pass\n", "python")
    assert all(not n.is_test for n in nodes)
    nodes, _ = _extract("tests/test_core.py", "def test_like():\n    pass\n", "python")
    assert any(n.kind == "Test" for n in nodes)


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
