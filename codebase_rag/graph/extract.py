"""Tree-sitter symbol extraction.

Deliberately thin. Upstream code-review-graph ships a 578KB parser covering
~45 languages; this covers the four that matter for our review targets and
stays readable. Adding a language means one entry in LANGUAGE_SPECS.

Call targets are extracted *unresolved* (bare name as written at the call
site) and resolved to qualified names later, in link.py, once every file in
the repo has been seen.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from .store import GraphEdge, GraphNode

EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
}


@dataclass(frozen=True)
class LanguageSpec:
    """Which AST node types carry which meaning, per grammar."""

    functions: frozenset[str]
    classes: frozenset[str]
    types: frozenset[str]
    calls: frozenset[str]
    imports: frozenset[str]
    # Node types whose `name` field holds a bound function expression,
    # e.g. `const handler = () => {}` in JS.
    bindings: frozenset[str] = frozenset()


LANGUAGE_SPECS: dict[str, LanguageSpec] = {
    "python": LanguageSpec(
        functions=frozenset({"function_definition"}),
        classes=frozenset({"class_definition"}),
        types=frozenset(),
        calls=frozenset({"call"}),
        imports=frozenset({"import_statement", "import_from_statement"}),
    ),
    "javascript": LanguageSpec(
        functions=frozenset({
            "function_declaration", "generator_function_declaration", "method_definition",
        }),
        classes=frozenset({"class_declaration"}),
        types=frozenset(),
        calls=frozenset({"call_expression", "new_expression"}),
        imports=frozenset({"import_statement"}),
        bindings=frozenset({"variable_declarator"}),
    ),
    "typescript": LanguageSpec(
        functions=frozenset({
            "function_declaration", "generator_function_declaration", "method_definition",
            "function_signature", "method_signature",
        }),
        classes=frozenset({"class_declaration", "abstract_class_declaration"}),
        types=frozenset({"interface_declaration", "type_alias_declaration", "enum_declaration"}),
        calls=frozenset({"call_expression", "new_expression"}),
        imports=frozenset({"import_statement"}),
        bindings=frozenset({"variable_declarator"}),
    ),
    "go": LanguageSpec(
        functions=frozenset({"function_declaration", "method_declaration"}),
        classes=frozenset(),
        types=frozenset({"type_spec"}),
        calls=frozenset({"call_expression"}),
        imports=frozenset({"import_spec"}),
    ),
}
LANGUAGE_SPECS["tsx"] = LANGUAGE_SPECS["typescript"]

# Callees this common are noise, not signal — they'd dominate every graph.
NOISY_CALLEES = frozenset({
    "print", "len", "str", "int", "float", "bool", "list", "dict", "set", "tuple",
    "range", "enumerate", "zip", "map", "filter", "sorted", "sum", "min", "max",
    "isinstance", "getattr", "setattr", "hasattr", "super", "type", "repr",
    "append", "get", "keys", "values", "items", "join", "split", "strip", "format",
    "log", "error", "warn", "info", "debug", "push", "pop", "slice", "toString",
    "console", "require", "make", "new", "append", "len", "cap", "panic", "recover",
})

# Directory names that mark everything below them as test code. Matched on
# whole path segments: `inspector/` or `latest/` must not count as tests.
_TEST_DIR_NAMES = frozenset({"test", "tests", "__tests__", "spec", "specs"})
_TEST_FILE_SUFFIXES = (
    "_test.py", "_test.go",
    ".test.ts", ".test.js", ".test.tsx", ".test.jsx", ".test.mjs", ".test.cjs",
)

# Signature-only declarations (TS overloads). A later declaration with a body
# under the same qualified name replaces the signature's node.
_SIGNATURE_TYPES = frozenset({"function_signature", "method_signature"})

# Receivers that refer to the enclosing object, not a module or variable the
# link pass could resolve through imports.
_SELF_RECEIVERS = frozenset({"self", "cls"})

# Marks a CALLS edge as `x.foo()` rather than `foo()`. link.py uses it to
# decide how much evidence is needed before binding the name to a symbol.
RECEIVER_CALL_CONFIDENCE = 0.25

_parser_cache: dict[str, object] = {}


def language_for(path: str | Path) -> str | None:
    return EXTENSION_TO_LANGUAGE.get(Path(path).suffix.lower())


def _get_parser(language: str):
    if language not in _parser_cache:
        from tree_sitter_language_pack import get_parser

        _parser_cache[language] = get_parser(language)
    return _parser_cache[language]


def _text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _name_of(node, source: bytes) -> str | None:
    field = node.child_by_field_name("name")
    if field is not None:
        return _text(field, source)
    return None


def _is_test_path(file_path: str) -> bool:
    parts = Path(file_path.lower().replace("\\", "/")).parts
    if not parts:
        return False
    name = parts[-1]
    return (
        name.startswith("test_")
        or name == "conftest.py"
        or name.endswith(_TEST_FILE_SUFFIXES)
        or ".spec." in name
        or any(part in _TEST_DIR_NAMES for part in parts[:-1])
    )


def _receiver_name(func, source: bytes) -> str | None:
    """`x` in `x.foo()` when x is a plain identifier, else None.

    The link pass binds `mod.f()` / Go `pkg.F()` through the file's imports,
    so only a bare name is useful. `self.x()`, `this.x()` and chained or
    computed objects (`a.b.c()`, `f().g()`) yield None.
    """
    obj = func.child_by_field_name("object") or func.child_by_field_name("operand")
    if obj is None or obj.type != "identifier":
        return None
    text = _text(obj, source)
    return None if text in _SELF_RECEIVERS else text


def _go_receiver_type(node, source: bytes) -> str | None:
    """`A` for `func (a *A[T]) m()`: receiver type, pointer/generics stripped."""
    receiver = node.child_by_field_name("receiver")
    if receiver is None:
        return None
    stack = [receiver]
    while stack:
        cur = stack.pop()
        if cur.type == "type_identifier":
            return _text(cur, source)
        stack.extend(reversed(cur.named_children))
    return None


def _callee_name(
    call_node, source: bytes, language: str
) -> tuple[str, bool, str | None] | None:
    """Callee name at a call site, whether it was called on a receiver, and
    the receiver's name when it is a plain identifier.

    Returns `(name, on_receiver, receiver)`. `foo()` is a bare call; `x.foo()`
    is a call on a receiver, and we do not know x's type. That distinction
    matters at link time: `classList.add(...)` must not bind to some unrelated
    top-level `add` just because the names match.
    """
    func = call_node.child_by_field_name("function") or call_node.child_by_field_name(
        "constructor"
    )
    if func is None:
        return None
    if func.type in ("identifier", "type_identifier"):
        return _text(func, source), False, None
    # attribute / member_expression / selector_expression
    attr = (
        func.child_by_field_name("attribute")
        or func.child_by_field_name("property")
        or func.child_by_field_name("field")
    )
    if attr is not None:
        return _text(attr, source), True, _receiver_name(func, source)
    if func.named_child_count:
        last = func.named_children[-1]
        if last.type in ("identifier", "property_identifier", "field_identifier"):
            return _text(last, source), True, None
    return None


def _import_targets(node, source: bytes, language: str) -> list[str]:
    """Module paths named by an import statement."""
    out: list[str] = []
    if language == "python":
        module = node.child_by_field_name("module_name")
        if module is not None:
            out.append(_text(module, source))
        else:
            for child in node.named_children:
                if child.type in ("dotted_name", "relative_import"):
                    out.append(_text(child, source))
                elif child.type == "aliased_import":
                    # `import a.b as c`: the module is `a.b`; link derives
                    # the alias from the path, so it is not recorded here.
                    name = child.child_by_field_name("name")
                    if name is not None:
                        out.append(_text(name, source))
    else:
        source_field = node.child_by_field_name("source") or node.child_by_field_name("path")
        if source_field is not None:
            out.append(_text(source_field, source).strip("\"'`"))
        elif node.named_child_count:
            out.append(_text(node.named_children[0], source).strip("\"'`"))
    return [t for t in out if t]


def _binding_is_function(node, source: bytes) -> bool:
    """True for `const f = () => {}` / `= function () {}`, false for plain data."""
    value = node.child_by_field_name("value")
    return value is not None and value.type in (
        "arrow_function", "function_expression", "function", "generator_function",
    )


def extract_file(
    file_path: str,
    source: bytes,
    language: str,
) -> tuple[list[GraphNode], list[GraphEdge]]:
    """Parse one file into graph nodes and (unresolved) edges."""
    spec = LANGUAGE_SPECS.get(language)
    if spec is None:
        return [], []

    try:
        tree = _get_parser(language).parse(source)
    except Exception as exc:  # grammar load / parse failure shouldn't kill a build
        logger.warning(f"parse failed for {file_path}: {exc}")
        return [], []

    is_test_file = _is_test_path(file_path)
    file_qn = file_path
    nodes: list[GraphNode] = [
        GraphNode(
            kind="File",
            name=Path(file_path).name,
            qualified_name=file_qn,
            file_path=file_path,
            line_start=1,
            line_end=source.count(b"\n") + 1,
            language=language,
            is_test=is_test_file,
        )
    ]
    edges: list[GraphEdge] = []
    seen_qns: set[str] = {file_qn}
    # qualified name -> index in `nodes`, for signature-only nodes that a
    # later implementation may replace.
    signature_nodes: dict[str, int] = {}

    def qualify(name: str, parent: str | None) -> str:
        return f"{file_path}::{parent}.{name}" if parent else f"{file_path}::{name}"

    def walk(node, scope_qn: str, scope_name: str | None) -> None:
        """Depth-first walk carrying the enclosing symbol as scope.

        `scope_name` is the enclosing symbol's full in-file dotted path
        (`A.run`), so `f"{file_path}::{scope_name}"` is its qualified name.
        """
        for child in node.named_children:
            kind = None
            name = None

            if child.type in spec.functions:
                kind, name = "Function", _name_of(child, source)
            elif child.type in spec.classes:
                kind, name = "Class", _name_of(child, source)
            elif child.type in spec.types:
                kind, name = "Type", _name_of(child, source)
            elif child.type in spec.bindings and _binding_is_function(child, source):
                kind, name = "Function", _name_of(child, source)

            if kind and name:
                parent = scope_name
                if child.type == "method_declaration" and language == "go":
                    # Go methods live at file level; scope them by receiver
                    # type so `(a *A) Close` and `(b *B) Close` don't collide.
                    parent = _go_receiver_type(child, source) or scope_name
                path = f"{parent}.{name}" if parent else name
                qn = qualify(name, parent)
                is_test = is_test_file and (
                    name.startswith("test") or name.startswith("Test") or "should" in name
                )
                node = GraphNode(
                    kind="Test" if is_test else kind,
                    name=name,
                    qualified_name=qn,
                    file_path=file_path,
                    line_start=child.start_point[0] + 1,
                    line_end=child.end_point[0] + 1,
                    language=language,
                    parent_name=parent,
                    is_test=is_test,
                )
                if qn in seen_qns:
                    # Overloads / redefinitions share a qualified name; the
                    # UNIQUE index would reject the second insert. A TS
                    # implementation replaces the overload signature before it.
                    if qn in signature_nodes and child.type not in _SIGNATURE_TYPES:
                        nodes[signature_nodes.pop(qn)] = node
                    walk(child, qn, path)
                    continue
                seen_qns.add(qn)
                if child.type in _SIGNATURE_TYPES:
                    signature_nodes[qn] = len(nodes)
                nodes.append(node)
                edges.append(
                    GraphEdge(
                        kind="CONTAINS",
                        source_qualified=scope_qn,
                        target_qualified=qn,
                        file_path=file_path,
                        line=child.start_point[0] + 1,
                    )
                )
                # Superclasses: unresolved, linked later.
                supers = child.child_by_field_name("superclasses") or child.child_by_field_name(
                    "heritage"
                )
                if supers is not None:
                    for sup in supers.named_children:
                        base = _text(sup, source).strip()
                        if base and base.isidentifier():
                            edges.append(
                                GraphEdge(
                                    kind="INHERITS",
                                    source_qualified=qn,
                                    target_qualified=base,
                                    file_path=file_path,
                                    line=child.start_point[0] + 1,
                                    confidence=0.5,
                                )
                            )
                walk(child, qn, path)
                continue

            if child.type in spec.calls:
                found = _callee_name(child, source, language)
                callee, on_receiver, receiver = found if found else (None, False, None)
                if callee and callee not in NOISY_CALLEES and len(callee) > 2:
                    edges.append(
                        GraphEdge(
                            kind="CALLS",
                            source_qualified=scope_qn,
                            target_qualified=callee,
                            file_path=file_path,
                            line=child.start_point[0] + 1,
                            # The link pass reads this back: a receiver call is
                            # weaker evidence and gets resolved more strictly.
                            confidence=RECEIVER_CALL_CONFIDENCE if on_receiver else 0.5,
                            receiver=receiver,
                        )
                    )
                walk(child, scope_qn, scope_name)
                continue

            if child.type in spec.imports:
                for target in _import_targets(child, source, language):
                    edges.append(
                        GraphEdge(
                            kind="IMPORTS_FROM",
                            source_qualified=file_qn,
                            target_qualified=target,
                            file_path=file_path,
                            line=child.start_point[0] + 1,
                            confidence=0.5,
                        )
                    )
                continue

            walk(child, scope_qn, scope_name)

    walk(tree.root_node, file_qn, None)
    return nodes, edges
