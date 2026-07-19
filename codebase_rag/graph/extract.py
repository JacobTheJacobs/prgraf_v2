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

_TEST_PATH_HINTS = ("test", "spec", "__tests__")

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
    lowered = file_path.lower()
    name = Path(lowered).name
    return (
        name.startswith("test_")
        or name.endswith(("_test.py", "_test.go", ".test.ts", ".test.js", ".spec.ts", ".spec.js"))
        or any(hint in lowered for hint in _TEST_PATH_HINTS)
    )


def _callee_name(call_node, source: bytes, language: str) -> str | None:
    """Bare name at a call site.

    We take the rightmost identifier: `a.b.c()` yields `c`. Precision is
    recovered in link.py, which prefers same-file then same-directory
    candidates when a name is ambiguous.
    """
    func = call_node.child_by_field_name("function") or call_node.child_by_field_name(
        "constructor"
    )
    if func is None:
        return None
    if func.type in ("identifier", "type_identifier"):
        return _text(func, source)
    # attribute / member_expression / selector_expression
    attr = (
        func.child_by_field_name("attribute")
        or func.child_by_field_name("property")
        or func.child_by_field_name("field")
    )
    if attr is not None:
        return _text(attr, source)
    if func.named_child_count:
        last = func.named_children[-1]
        if last.type in ("identifier", "property_identifier", "field_identifier"):
            return _text(last, source)
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

    def qualify(name: str, parent: str | None) -> str:
        return f"{file_path}::{parent}.{name}" if parent else f"{file_path}::{name}"

    def walk(node, scope_qn: str, scope_name: str | None) -> None:
        """Depth-first walk carrying the enclosing symbol as scope."""
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
                qn = qualify(name, scope_name)
                if qn in seen_qns:
                    # Overloads / redefinitions share a qualified name; the
                    # UNIQUE index would reject the second insert.
                    walk(child, qn, name)
                    continue
                seen_qns.add(qn)
                is_test = is_test_file and (
                    name.startswith("test") or name.startswith("Test") or "should" in name
                )
                nodes.append(
                    GraphNode(
                        kind="Test" if is_test else kind,
                        name=name,
                        qualified_name=qn,
                        file_path=file_path,
                        line_start=child.start_point[0] + 1,
                        line_end=child.end_point[0] + 1,
                        language=language,
                        parent_name=scope_name,
                        is_test=is_test,
                    )
                )
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
                walk(child, qn, name)
                continue

            if child.type in spec.calls:
                callee = _callee_name(child, source, language)
                if callee and callee not in NOISY_CALLEES and len(callee) > 2:
                    edges.append(
                        GraphEdge(
                            kind="CALLS",
                            source_qualified=scope_qn,
                            target_qualified=callee,
                            file_path=file_path,
                            line=child.start_point[0] + 1,
                            confidence=0.5,
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
