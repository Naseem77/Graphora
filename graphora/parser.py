"""Deterministic source parsing with tree-sitter (regex fallback).

No LLM anywhere in this module. Every extracted fact carries a confidence
tag so downstream consumers can tell what was read directly from source
(EXTRACTED), what was resolved by name matching (INFERRED), and what is
uncertain (AMBIGUOUS).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    from tree_sitter import Language, Parser
except ImportError:  # pragma: no cover - optional parser dependency
    Language = None  # type: ignore[assignment]
    Parser = None  # type: ignore[assignment]


EXTRACTED = "EXTRACTED"
INFERRED = "INFERRED"
AMBIGUOUS = "AMBIGUOUS"

DOC_EXTENSIONS = {".md", ".mdx"}
CODE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".java",
    ".rs", ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".rb", ".php",
}

# Languages with regex symbol patterns; used for diff attribution too.
REGEX_LANGUAGES = ("python", "typescript", "go", "java", "rust", "c", "cpp", "ruby", "php")

_TEST_PATH_PATTERN = re.compile(r"(^|/)(tests?|__tests__|spec)(/|_)|(_test\.|\.test\.|\.spec\.|^test_)")


@dataclass(frozen=True)
class ParsedSymbol:
    kind: str
    name: str
    line: int
    signature: str
    confidence: str = EXTRACTED
    qualname: str = ""  # dotted path through enclosing scopes, e.g. "Widget.render"
    end_line: int = 0
    parent: str = ""  # qualified name of the enclosing scope ("" at top level)
    parent_kind: str = ""  # "Class", "Function", "Namespace", or ""

    def __post_init__(self) -> None:
        if not self.qualname:
            object.__setattr__(self, "qualname", self.name)
        if not self.end_line:
            object.__setattr__(self, "end_line", self.line)

    @property
    def is_method(self) -> bool:
        return self.kind == "Function" and self.parent_kind == "Class"


@dataclass(frozen=True)
class ParsedCall:
    caller: str  # qualified name of the calling function
    callee: str  # called name, without its receiver
    line: int
    confidence: str = INFERRED
    receiver: str = ""  # "" for a bare call; otherwise the text before the name ("self", "os.path")


@dataclass(frozen=True)
class ParsedImport:
    name: str
    confidence: str = EXTRACTED


@dataclass(frozen=True)
class ParsedBinding:
    """A name an import makes visible in a file.

    `local` is the name used in the file, `module` the module or path as
    written, and `name` the imported member: "" when `local` is the module
    itself, "*" for a wildcard import or #include.
    """

    local: str
    module: str
    name: str = ""


@dataclass(frozen=True)
class ParsedFile:
    path: str
    language: str
    symbols: list[ParsedSymbol]
    imports: list[ParsedImport]
    calls: list[ParsedCall]
    source_text: str
    is_test: bool = False
    parser_used: str = "tree-sitter"
    bindings: list[ParsedBinding] = field(default_factory=list)


def is_supported_code_file(path: str) -> bool:
    return Path(path).suffix.lower() in CODE_EXTENSIONS


def is_test_path(path: str) -> bool:
    return bool(_TEST_PATH_PATTERN.search(path.replace("\\", "/").lower()))


def language_for_path(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".go": "go",
        ".java": "java",
        ".rs": "rust",
        ".c": "c",
        ".h": "c",
        ".cpp": "cpp",
        ".cc": "cpp",
        ".cxx": "cpp",
        ".hpp": "cpp",
        ".hh": "cpp",
        ".rb": "ruby",
        ".php": "php",
    }.get(suffix, "text")


def parse_code_file(path: str, content: str) -> ParsedFile:
    """Parse one source file. Tree-sitter first, regex fallback."""
    language = language_for_path(path)
    parsed = _parse_with_tree_sitter(path, content, language)
    if parsed is not None:
        return parsed
    return _parse_with_regex(path, content, language)


def extract_symbols_from_diff(diff: str) -> list[str]:
    """Names of symbols defined on added/removed diff lines.

    Each line is matched only with the definition patterns of its file's
    language (from the ---/+++ headers); C's patterns used to read Python's
    `return len(` as a definition of `len`.
    """
    symbols: list[str] = []
    seen: set[str] = set()
    languages: tuple[str, ...] = REGEX_LANGUAGES
    for line in diff.splitlines():
        if line.startswith(("+++ ", "--- ")):
            target = line[4:].strip()
            if target != "/dev/null":
                language = language_for_path(target.removeprefix("a/").removeprefix("b/"))
                languages = ("typescript",) if language == "javascript" else (language,)
                if language == "text":
                    languages = REGEX_LANGUAGES
            continue
        if not line.startswith(("+", "-")):
            continue
        stripped = line[1:].strip()
        for language in languages:
            for _, name in _extract_symbols(language, stripped):
                if name not in seen:
                    seen.add(name)
                    symbols.append(name)
    return symbols


def extract_files_from_diff(diff: str) -> list[str]:
    """File paths touched by a unified diff."""
    files: list[str] = []
    for line in diff.splitlines():
        match = re.match(r"^\+\+\+ (?:b/)?(.+)$", line)
        if match and match.group(1) != "/dev/null" and match.group(1) not in files:
            files.append(match.group(1))
    return files


# --- tree-sitter path -------------------------------------------------------

# Values that make a variable, property, or class field a function definition.
_JS_FUNCTION_VALUES = {"arrow_function", "function_expression", "function", "generator_function"}
_NAME_PATTERN = re.compile(r"^~?[A-Za-z_][\w$]*[?!=]?$")
_UNKNOWN_QUALIFIER_KIND = "?"


@dataclass(frozen=True)
class _Definition:
    kind: str  # "Function", "Class", or "Namespace"
    name: str
    emit: bool = True  # False: opens a scope without being a symbol (impl blocks, namespaces)
    qualifiers: tuple[str, ...] = ()  # scope named in the definition itself (C++ A::f, Go receivers)
    qualifier_kind: str = ""


@dataclass(frozen=True)
class _Frame:
    qualname: str
    kind: str


def _parse_with_tree_sitter(path: str, content: str, language: str) -> ParsedFile | None:
    grammar = "tsx" if path.lower().endswith(".tsx") else language
    parser = _tree_sitter_parser(grammar)
    if parser is None:
        return None

    content_bytes = content.encode("utf-8")
    root = parser.parse(content_bytes).root_node
    symbols: list[ParsedSymbol] = []
    imports: list[ParsedImport] = []
    bindings: list[ParsedBinding] = []
    calls: list[ParsedCall] = []

    # Iterative pre-order walk carrying the enclosing scopes (deep trees overflow recursion).
    stack: list[tuple[Any, tuple[_Frame, ...]]] = [(root, ())]
    while stack:
        node, scope = stack.pop()
        child_scope = scope

        imported = _tree_sitter_import(language, node, content_bytes)
        if imported:
            imports.append(ParsedImport(name=imported, confidence=EXTRACTED))
        bindings.extend(_tree_sitter_bindings(language, node, content_bytes))

        definition = _tree_sitter_definition(language, node, content_bytes)
        if definition is not None:
            parent = scope[-1].qualname if scope else ""
            parent_kind = scope[-1].kind if scope else ""
            if definition.qualifiers:
                parent = ".".join(part for part in (parent, *definition.qualifiers) if part)
                parent_kind = definition.qualifier_kind
            qualname = f"{parent}.{definition.name}" if parent else definition.name
            if definition.emit:
                symbols.append(
                    ParsedSymbol(
                        kind=definition.kind,
                        name=definition.name,
                        line=_point_row(node.start_point) + 1,
                        signature=_source_line_at(content_bytes, node.start_byte),
                        confidence=EXTRACTED,
                        qualname=qualname,
                        end_line=_point_row(node.end_point) + 1,
                        parent=parent,
                        parent_kind=parent_kind,
                    )
                )
            child_scope = scope + (_Frame(qualname, definition.kind),)

        call = _tree_sitter_call(language, node, content_bytes)
        if call is not None:
            callee, receiver = call
            caller = next((f.qualname for f in reversed(scope) if f.kind == "Function"), None)
            # Self-recursion adds no impact information; drop it as before.
            if caller and not (receiver == "" and caller.rsplit(".", 1)[-1] == callee):
                line = _point_row(node.start_point) + 1
                calls.append(ParsedCall(caller=caller, callee=callee, line=line, confidence=INFERRED, receiver=receiver))

        stack.extend((child, child_scope) for child in reversed(node.children))

    symbols = _settle_qualifier_kinds(symbols)

    # Calls to a function defined in the same file are certain: promote to EXTRACTED.
    local_functions = {s.name for s in symbols if s.kind == "Function"}
    calls = [
        ParsedCall(c.caller, c.callee, c.line, EXTRACTED if c.callee in local_functions else INFERRED, c.receiver)
        for c in calls
    ]

    return ParsedFile(
        path=path,
        language=language,
        symbols=symbols,
        imports=_dedupe_imports(imports),
        calls=_dedupe_calls(calls),
        source_text=content,
        is_test=is_test_path(path),
        parser_used="tree-sitter",
        bindings=_dedupe_bindings(bindings),
    )


def _settle_qualifier_kinds(symbols: list[ParsedSymbol]) -> list[ParsedSymbol]:
    """C++ `A::f` names a class or a namespace; decide by whether A is a class here."""
    if not any(s.parent_kind == _UNKNOWN_QUALIFIER_KIND for s in symbols):
        return symbols
    classes = {s.qualname for s in symbols if s.kind == "Class"}
    classes |= {s.name for s in symbols if s.kind == "Class"}
    settled = []
    for s in symbols:
        if s.parent_kind == _UNKNOWN_QUALIFIER_KIND:
            kind = "Class" if s.parent in classes or s.parent.rsplit(".", 1)[-1] in classes else "Namespace"
            s = replace(s, parent_kind=kind)
        settled.append(s)
    return settled


@lru_cache(maxsize=None)
def _tree_sitter_parser(language: str) -> Any | None:
    if Parser is None or Language is None:
        return None
    module_name, language_attr = {
        "python": ("tree_sitter_python", "language"),
        "javascript": ("tree_sitter_javascript", "language"),
        "typescript": ("tree_sitter_typescript", "language_typescript"),
        "tsx": ("tree_sitter_typescript", "language_tsx"),
        "go": ("tree_sitter_go", "language"),
        "java": ("tree_sitter_java", "language"),
        "rust": ("tree_sitter_rust", "language"),
        "c": ("tree_sitter_c", "language"),
        "cpp": ("tree_sitter_cpp", "language"),
        "ruby": ("tree_sitter_ruby", "language"),
        "php": ("tree_sitter_php", "language_php"),
    }.get(language, (None, None))
    if not module_name:
        return None
    try:
        module = __import__(module_name)
        parser = Parser()
        parser.language = Language(getattr(module, language_attr)())
        return parser
    except (ImportError, AttributeError, TypeError, ValueError):
        return None


def _point_row(point: Any) -> int:
    """Read a tree-sitter ``Point``'s row via tuple/index access.

    tree-sitter 0.26.0 has a known upstream bug (already fixed, not yet
    released) where the named ``Point.row``/``Point.column`` attributes
    return unstable values for source coordinates beyond the small-integer
    range, while tuple/index access (``point[0]``, ``point[1]``) remains
    stable across affected and unaffected versions alike. Always read the
    row this way rather than via ``point.row``.
    """
    return point[0]


def _node_text(node: Any | None, content: bytes) -> str:
    if node is None:
        return ""
    return content[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")


def _field_children(node: Any, field_name: str) -> list[Any]:
    return [child for i, child in enumerate(node.children) if node.field_name_for_child(i) == field_name]


def _source_line_at(content: bytes, offset: int) -> str:
    """The stripped source line containing byte `offset`.

    Splits on newline bytes only, matching tree-sitter's row numbering;
    str.splitlines() also breaks on form feeds and Unicode separators, and
    re-splitting the whole file per symbol made parsing quadratic.
    """
    start = content.rfind(b"\n", 0, offset) + 1
    end = content.find(b"\n", offset)
    if end == -1:
        end = len(content)
    return content[start:end].decode("utf-8", errors="replace").strip()


def _tree_sitter_import(language: str, node: Any, content: bytes) -> str | None:
    if language == "python" and node.type in {"import_statement", "import_from_statement"}:
        return _extract_import(language, _node_text(node, content).strip())
    if language in {"javascript", "typescript"} and node.type == "import_statement":
        return _extract_import(language, _node_text(node, content).strip())
    if language == "go" and node.type == "import_spec":
        return _node_text(node.child_by_field_name("path"), content).strip("\"'")
    if language == "java" and node.type == "import_declaration":
        text = _node_text(node, content).strip()
        return text.removeprefix("import ").removesuffix(";").strip()
    if language == "rust" and node.type == "use_declaration":
        text = _node_text(node, content).strip()
        return text.removeprefix("use ").removesuffix(";").strip()
    if language in {"c", "cpp"} and node.type == "preproc_include":
        path_node = node.child_by_field_name("path")
        return _node_text(path_node, content).strip('"<>') or None
    if language == "ruby" and node.type == "call":
        method = _node_text(node.child_by_field_name("method"), content)
        if method in {"require", "require_relative"}:
            args = node.child_by_field_name("arguments")
            return _node_text(args, content).strip("()\"' ") or None
        return None
    if language == "php" and node.type == "namespace_use_declaration":
        text = _node_text(node, content).strip()
        return text.removeprefix("use ").removesuffix(";").strip() or None
    return None


# --- definitions --------------------------------------------------------------


def _tree_sitter_definition(language: str, node: Any, content: bytes) -> _Definition | None:
    handler = _DEFINITION_HANDLERS.get(language)
    return handler(node, content) if handler else None


def _named(kind: str, node: Any, content: bytes, field_name: str = "name", **extra: Any) -> _Definition | None:
    name = _node_text(node.child_by_field_name(field_name), content).strip()
    if not _NAME_PATTERN.match(name):
        return None
    return _Definition(kind=kind, name=name, **extra)


def _python_definition(node: Any, content: bytes) -> _Definition | None:
    if node.type == "function_definition":
        return _named("Function", node, content)
    if node.type == "class_definition":
        return _named("Class", node, content)
    if node.type == "assignment":
        left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
        if left is not None and right is not None and left.type == "identifier" and right.type == "lambda":
            return _named("Function", node, content, field_name="left")
    return None


def _js_definition(node: Any, content: bytes) -> _Definition | None:
    kind = node.type
    if kind in {"function_declaration", "generator_function_declaration", "method_definition"}:
        return _named("Function", node, content)
    if kind in {"class_declaration", "abstract_class_declaration", "class", "interface_declaration", "enum_declaration"}:
        return _named("Class", node, content)
    if kind in {"internal_module", "module"}:
        return _named("Namespace", node, content, emit=False)
    value = node.child_by_field_name("value")
    if value is None:
        return None
    if kind == "variable_declarator":
        name_node = node.child_by_field_name("name")
        if name_node is None or name_node.type != "identifier":
            return None
        if value.type in _JS_FUNCTION_VALUES:
            return _named("Function", node, content)
        if value.type == "object":
            return _named("Namespace", node, content, emit=False)
        return None
    if value.type not in _JS_FUNCTION_VALUES:
        return None
    if kind == "pair":
        name = _node_text(node.child_by_field_name("key"), content).strip("\"'`")
        return _Definition("Function", name) if _NAME_PATTERN.match(name) else None
    if kind == "field_definition":
        return _named("Function", node, content, field_name="property")
    if kind == "public_field_definition":
        return _named("Function", node, content)
    return None


def _go_definition(node: Any, content: bytes) -> _Definition | None:
    if node.type == "function_declaration":
        return _named("Function", node, content)
    if node.type == "method_declaration":
        receiver = node.child_by_field_name("receiver")
        receiver_type = ""
        if receiver is not None:
            for param in receiver.named_children:
                type_text = _node_text(param.child_by_field_name("type"), content)
                receiver_type = type_text.lstrip("*").split("[")[0].strip()
        qualifiers = (receiver_type,) if receiver_type else ()
        return _named("Function", node, content, qualifiers=qualifiers, qualifier_kind="Class")
    if node.type == "type_spec":
        return _named("Class", node, content)
    return None


def _java_definition(node: Any, content: bytes) -> _Definition | None:
    if node.type == "method_declaration":
        return _named("Function", node, content) if node.child_by_field_name("body") is not None else None
    if node.type in {"constructor_declaration", "compact_constructor_declaration"}:
        return _named("Function", node, content)
    if node.type in {
        "class_declaration", "interface_declaration", "enum_declaration",
        "record_declaration", "annotation_type_declaration",
    }:
        return _named("Class", node, content)
    return None


def _rust_definition(node: Any, content: bytes) -> _Definition | None:
    if node.type == "function_item":
        return _named("Function", node, content)
    if node.type in {"struct_item", "enum_item", "trait_item", "union_item"}:
        return _named("Class", node, content)
    if node.type == "impl_item":
        type_text = _node_text(node.child_by_field_name("type"), content)
        name = type_text.split("<")[0].split("::")[-1].strip()
        return _Definition("Class", name, emit=False) if _NAME_PATTERN.match(name) else None
    if node.type == "mod_item":
        return _named("Namespace", node, content, emit=False)
    return None


def _c_family_definition(node: Any, content: bytes, cpp: bool) -> _Definition | None:
    if node.type == "function_definition":
        name_node = _c_declarator_name(node)
        if name_node is None:
            return None
        qualifiers: tuple[str, ...] = ()
        if name_node.type == "qualified_identifier":
            scope_text = _node_text(name_node.child_by_field_name("scope"), content)
            qualifiers = tuple(part for part in scope_text.split("::") if part)
            name_node = _innermost_name(name_node)
        name = _node_text(name_node, content).strip()
        if not _NAME_PATTERN.match(name):
            return None
        if qualifiers:
            return _Definition("Function", name, qualifiers=qualifiers, qualifier_kind=_UNKNOWN_QUALIFIER_KIND)
        return _Definition("Function", name)
    type_specifiers = {"struct_specifier", "union_specifier", "enum_specifier"} | ({"class_specifier"} if cpp else set())
    if node.type in type_specifiers and node.child_by_field_name("body") is not None:
        return _named("Class", node, content)
    if node.type == "type_definition":
        inner = node.child_by_field_name("type")
        if (
            inner is not None
            and inner.type in type_specifiers
            and inner.child_by_field_name("body") is not None
            and inner.child_by_field_name("name") is None
        ):
            return _named("Class", node, content, field_name="declarator")
    if cpp and node.type == "namespace_definition":
        return _named("Namespace", node, content, emit=False)
    return None


def _innermost_name(node: Any) -> Any:
    """`a::B::f` nests qualified_identifiers; return the final name node."""
    while node is not None and node.type == "qualified_identifier":
        node = node.child_by_field_name("name")
    return node


def _ruby_definition(node: Any, content: bytes) -> _Definition | None:
    if node.type in {"method", "singleton_method"}:
        return _named("Function", node, content)
    if node.type in {"class", "module"}:
        parts = _node_text(node.child_by_field_name("name"), content).split("::")
        name = parts[-1].strip()
        if not _NAME_PATTERN.match(name):
            return None
        return _Definition("Class", name, qualifiers=tuple(parts[:-1]), qualifier_kind="Class" if len(parts) > 1 else "")
    return None


def _php_definition(node: Any, content: bytes) -> _Definition | None:
    if node.type == "function_definition":
        return _named("Function", node, content)
    if node.type == "method_declaration":
        return _named("Function", node, content) if node.child_by_field_name("body") is not None else None
    if node.type in {"class_declaration", "interface_declaration", "trait_declaration", "enum_declaration"}:
        return _named("Class", node, content)
    return None


_DEFINITION_HANDLERS = {
    "python": _python_definition,
    "javascript": _js_definition,
    "typescript": _js_definition,
    "go": _go_definition,
    "java": _java_definition,
    "rust": _rust_definition,
    "c": lambda node, content: _c_family_definition(node, content, cpp=False),
    "cpp": lambda node, content: _c_family_definition(node, content, cpp=True),
    "ruby": _ruby_definition,
    "php": _php_definition,
}


def _c_declarator_name(node: Any) -> Any | None:
    """Descend C/C++ declarators to the node that names the function."""
    declarator = node.child_by_field_name("declarator")
    depth = 0
    while declarator is not None and depth < 10:
        if declarator.type in {"identifier", "field_identifier", "destructor_name", "qualified_identifier"}:
            return declarator
        declarator = declarator.child_by_field_name("declarator")
        depth += 1
    return None


# --- calls --------------------------------------------------------------------


def _tree_sitter_call(language: str, node: Any, content: bytes) -> tuple[str, str] | None:
    """(callee name, receiver text) for a call node, else None.

    The receiver is "" for a bare call `f()`, and the text before the last
    `.`/`::`/`->` otherwise ("self", "os.path", "Foo"). Constructions such as
    `new Foo()` are reported as a bare call to `Foo`.
    """
    t = node.type
    field = node.child_by_field_name
    if language == "python" and t == "call":
        return _split_callee(field("function"), content, ("attribute", "object", "attribute"))
    if language in {"javascript", "typescript"}:
        if t == "call_expression":
            return _split_callee(field("function"), content, ("member_expression", "object", "property"))
        if t == "new_expression":
            return _bare(_node_text(field("constructor"), content))
        return None
    if language == "go" and t == "call_expression":
        return _split_callee(field("function"), content, ("selector_expression", "operand", "field"))
    if language == "java":
        if t == "method_invocation":
            return _with_receiver(_node_text(field("name"), content), _node_text(field("object"), content))
        if t == "object_creation_expression":
            return _bare(_node_text(field("type"), content).split("<")[0])
        return None
    if language == "rust" and t == "call_expression":
        function = field("function")
        if function is not None and function.type == "scoped_identifier":
            return _with_receiver(
                _node_text(function.child_by_field_name("name"), content),
                _node_text(function.child_by_field_name("path"), content),
            )
        return _split_callee(function, content, ("field_expression", "value", "field"))
    if language in {"c", "cpp"} and t == "call_expression":
        function = field("function")
        if function is not None and function.type == "qualified_identifier":
            return _with_receiver(
                _node_text(_innermost_name(function), content),
                _node_text(function.child_by_field_name("scope"), content),
            )
        return _split_callee(function, content, ("field_expression", "argument", "field"))
    if language == "ruby" and t == "call":
        method = _node_text(field("method"), content)
        receiver = _node_text(field("receiver"), content)
        if method in {"require", "require_relative"}:
            return None
        if method == "new":
            # Invoice.new(...) constructs Invoice; a bare `new` is Class.new on self.
            return _bare(receiver.split("::")[-1]) if receiver else None
        return _with_receiver(method, receiver)
    if language == "php":
        if t == "function_call_expression":
            return _bare(_node_text(field("function"), content).split("\\")[-1])
        if t == "member_call_expression":
            return _with_receiver(_node_text(field("name"), content), _node_text(field("object"), content))
        if t == "scoped_call_expression":
            return _with_receiver(_node_text(field("name"), content), _node_text(field("scope"), content))
        if t == "object_creation_expression":
            for child in node.named_children:
                if child.type in {"name", "qualified_name"}:
                    return _bare(_node_text(child, content).split("\\")[-1])
        return None
    return None


def _split_callee(function: Any | None, content: bytes, member: tuple[str, str, str]) -> tuple[str, str] | None:
    if function is None:
        return None
    member_type, object_field, name_field = member
    if function.type == member_type:
        return _with_receiver(
            _node_text(function.child_by_field_name(name_field), content),
            _node_text(function.child_by_field_name(object_field), content),
        )
    if function.type in {"identifier", "name"}:
        return _bare(_node_text(function, content))
    return None


# A capitalized constructor call used as a receiver; the name says its type.
_CONSTRUCTED_RECEIVER = re.compile(r"^(?:new\s+)?((?:[A-Za-z_]\w*(?:\.|::))*[A-Z]\w*)\s*\([^()]*\)$")


def _bare(name: str) -> tuple[str, str] | None:
    return _with_receiver(name, "")


def _with_receiver(name: str, receiver: str) -> tuple[str, str] | None:
    name = name.strip().lstrip("$&")
    if not _NAME_PATTERN.match(name):
        return None
    receiver = " ".join(receiver.split())
    if receiver.startswith("$"):
        receiver = receiver[1:]  # PHP $this / $obj
    constructed = _CONSTRUCTED_RECEIVER.match(receiver)
    if constructed:
        receiver = constructed.group(1)  # `Db().save()` / `new Db().save()`: the receiver is a Db
    if any(ch in receiver for ch in "()[]{}\"'") or len(receiver) > 120:
        receiver = "<expr>"  # call chains, subscripts: receiver type is unknown
    return name, receiver


# --- import bindings ----------------------------------------------------------


def _tree_sitter_bindings(language: str, node: Any, content: bytes) -> list[ParsedBinding]:
    handler = _BINDING_HANDLERS.get(language)
    return handler(node, content) if handler else []


def _python_bindings(node: Any, content: bytes) -> list[ParsedBinding]:
    if node.type == "import_statement":
        out = []
        for child in _field_children(node, "name"):
            if child.type == "aliased_import":
                module = _node_text(child.child_by_field_name("name"), content)
                out.append(ParsedBinding(_node_text(child.child_by_field_name("alias"), content), module))
            else:
                module = _node_text(child, content)
                out.append(ParsedBinding(module, module))
        return out
    if node.type == "import_from_statement":
        module = _node_text(node.child_by_field_name("module_name"), content).replace(" ", "")
        out = []
        if any(child.type == "wildcard_import" for child in node.named_children):
            out.append(ParsedBinding("*", module, "*"))
        for child in _field_children(node, "name"):
            if child.type == "aliased_import":
                name = _node_text(child.child_by_field_name("name"), content)
                out.append(ParsedBinding(_node_text(child.child_by_field_name("alias"), content), module, name))
            else:
                name = _node_text(child, content)
                out.append(ParsedBinding(name, module, name))
        return out
    return []


def _js_bindings(node: Any, content: bytes) -> list[ParsedBinding]:
    if node.type == "export_statement" and any(child.type == "default" for child in node.children):
        # Recorded on the exporting file as local "default" -> the exported name.
        declaration = node.child_by_field_name("declaration")
        value = node.child_by_field_name("value")
        name = _node_text(declaration.child_by_field_name("name"), content) if declaration is not None else ""
        if not name and value is not None and value.type == "identifier":
            name = _node_text(value, content)
        return [ParsedBinding("default", "", name)] if name else []
    if node.type == "import_statement":
        source = _node_text(node.child_by_field_name("source"), content).strip("\"'`")
        out = []
        for clause in node.named_children:
            if clause.type != "import_clause":
                continue
            for part in clause.named_children:
                if part.type == "identifier":
                    out.append(ParsedBinding(_node_text(part, content), source, "default"))
                elif part.type == "namespace_import":
                    for ident in part.named_children:
                        out.append(ParsedBinding(_node_text(ident, content), source))
                elif part.type == "named_imports":
                    for spec in part.named_children:
                        if spec.type != "import_specifier":
                            continue
                        name = _node_text(spec.child_by_field_name("name"), content)
                        alias = _node_text(spec.child_by_field_name("alias"), content) or name
                        out.append(ParsedBinding(alias, source, name))
        return out
    if node.type == "variable_declarator":
        value = node.child_by_field_name("value")
        if value is None or value.type != "call_expression":
            return []
        if _node_text(value.child_by_field_name("function"), content) != "require":
            return []
        args = value.child_by_field_name("arguments")
        strings = [c for c in (args.named_children if args is not None else []) if c.type == "string"]
        if not strings:
            return []
        source = _node_text(strings[0], content).strip("\"'`")
        target = node.child_by_field_name("name")
        if target is None:
            return []
        if target.type == "identifier":
            return [ParsedBinding(_node_text(target, content), source)]
        out = []
        for prop in target.named_children:
            if prop.type == "shorthand_property_identifier_pattern":
                name = _node_text(prop, content)
                out.append(ParsedBinding(name, source, name))
            elif prop.type == "pair_pattern":
                key = _node_text(prop.child_by_field_name("key"), content)
                out.append(ParsedBinding(_node_text(prop.child_by_field_name("value"), content), source, key))
        return out
    return []


def _go_bindings(node: Any, content: bytes) -> list[ParsedBinding]:
    if node.type != "import_spec":
        return []
    path = _node_text(node.child_by_field_name("path"), content).strip("\"`")
    alias = _node_text(node.child_by_field_name("name"), content)
    if alias == "_":
        return []
    if alias == ".":
        return [ParsedBinding("*", path, "*")]
    return [ParsedBinding(alias or path.rstrip("/").rsplit("/", 1)[-1], path)]


def _java_bindings(node: Any, content: bytes) -> list[ParsedBinding]:
    if node.type != "import_declaration":
        return []
    text = _node_text(node, content).strip().removeprefix("import").strip().removesuffix(";").strip()
    static = text.startswith("static ")
    text = text.removeprefix("static ").strip()
    if text.endswith(".*"):
        return [ParsedBinding("*", text[:-2], "*")]
    owner, _, last = text.rpartition(".")
    if static:
        return [ParsedBinding(last, owner, last)]
    return [ParsedBinding(last, text)]


def _rust_bindings(node: Any, content: bytes) -> list[ParsedBinding]:
    if node.type != "use_declaration":
        return []
    out: list[ParsedBinding] = []
    _rust_use_tree(node.child_by_field_name("argument"), "", content, out)
    return out


def _rust_use_tree(node: Any | None, prefix: str, content: bytes, out: list[ParsedBinding]) -> None:
    if node is None:
        return
    join = (lambda a, b: f"{a}::{b}" if a else b)
    if node.type in {"identifier", "self", "crate", "super"}:
        name = _node_text(node, content)
        out.append(ParsedBinding(name, prefix, name) if prefix else ParsedBinding(name, name))
    elif node.type == "scoped_identifier":
        path = _node_text(node.child_by_field_name("path"), content)
        name = _node_text(node.child_by_field_name("name"), content)
        out.append(ParsedBinding(name, join(prefix, path), name))
    elif node.type == "use_as_clause":
        path = _node_text(node.child_by_field_name("path"), content)
        alias = _node_text(node.child_by_field_name("alias"), content)
        owner, _, name = join(prefix, path).rpartition("::")
        out.append(ParsedBinding(alias, owner, name) if owner else ParsedBinding(alias, name))
    elif node.type == "scoped_use_list":
        path = _node_text(node.child_by_field_name("path"), content)
        for child in (node.child_by_field_name("list").named_children if node.child_by_field_name("list") else []):
            _rust_use_tree(child, join(prefix, path), content, out)
    elif node.type == "use_list":
        for child in node.named_children:
            _rust_use_tree(child, prefix, content, out)
    elif node.type == "use_wildcard":
        path = _node_text(node, content).removesuffix("::*").removesuffix("*")
        out.append(ParsedBinding("*", join(prefix, path), "*"))


def _c_bindings(node: Any, content: bytes) -> list[ParsedBinding]:
    if node.type != "preproc_include":
        return []
    path_node = node.child_by_field_name("path")
    if path_node is None or path_node.type == "system_lib_string":
        return []  # <stdio.h>: outside the repository
    return [ParsedBinding("*", _node_text(path_node, content).strip('"'), "*")]


def _ruby_bindings(node: Any, content: bytes) -> list[ParsedBinding]:
    if node.type != "call":
        return []
    method = _node_text(node.child_by_field_name("method"), content)
    if method not in {"require", "require_relative"}:
        return []
    target = _node_text(node.child_by_field_name("arguments"), content).strip("()\"' ")
    if not target:
        return []
    if method == "require_relative" and not target.startswith("."):
        target = f"./{target}"
    return [ParsedBinding("*", target, "*")]


def _php_bindings(node: Any, content: bytes) -> list[ParsedBinding]:
    if node.type != "namespace_use_clause":
        return []
    full = ""
    alias = _node_text(node.child_by_field_name("alias"), content)
    for child in node.named_children:
        if child.type in {"qualified_name", "name"} and node.field_name_for_child(node.children.index(child)) != "alias":
            full = _node_text(child, content).lstrip("\\")
            break
    if not full:
        return []
    return [ParsedBinding(alias or full.rsplit("\\", 1)[-1], full)]


_BINDING_HANDLERS = {
    "python": _python_bindings,
    "javascript": _js_bindings,
    "typescript": _js_bindings,
    "go": _go_bindings,
    "java": _java_bindings,
    "rust": _rust_bindings,
    "c": _c_bindings,
    "cpp": _c_bindings,
    "ruby": _ruby_bindings,
    "php": _php_bindings,
}


# --- regex fallback path ----------------------------------------------------


def _parse_with_regex(path: str, content: str, language: str) -> ParsedFile:
    symbols: list[ParsedSymbol] = []
    imports: list[ParsedImport] = []
    calls: list[ParsedCall] = []
    current_function: tuple[str, int] | None = None

    for line_number, line in enumerate(content.split("\n"), start=1):
        stripped = line.strip()
        if not stripped:
            continue

        import_name = _extract_import(language, stripped)
        if import_name:
            imports.append(ParsedImport(name=import_name, confidence=EXTRACTED))

        for kind, name in _extract_symbols(language, stripped):
            symbols.append(
                ParsedSymbol(kind=kind, name=name, line=line_number, signature=stripped, confidence=INFERRED)
            )
            if kind == "Function":
                current_function = (name, line_number)

        if current_function:
            for callee in _extract_calls(language, stripped):
                if callee != current_function[0]:
                    calls.append(
                        ParsedCall(caller=current_function[0], callee=callee, line=line_number, confidence=AMBIGUOUS)
                    )

    return ParsedFile(
        path=path,
        language=language,
        symbols=symbols,
        imports=_dedupe_imports(imports),
        calls=_dedupe_calls(calls),
        source_text=content,
        is_test=is_test_path(path),
        parser_used="regex",
    )


def _extract_import(language: str, line: str) -> str | None:
    if language == "python":
        match = re.match(r"(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", line)
        return _first_group(match)
    if language in {"javascript", "typescript"}:
        match = re.match(r"import\s+(?:.+?\s+from\s+)?['\"]([^'\"]+)['\"]", line)
        return _first_group(match)
    if language == "go":
        match = re.match(r"import\s+['\"]([^'\"]+)['\"]", line)
        return _first_group(match)
    if language == "java":
        match = re.match(r"import\s+([\w.]+);", line)
        return _first_group(match)
    if language == "rust":
        match = re.match(r"use\s+([\w:]+)", line)
        return _first_group(match)
    if language in {"c", "cpp"}:
        match = re.match(r"#include\s+[<\"]([^>\"]+)[>\"]", line)
        return _first_group(match)
    if language == "ruby":
        match = re.match(r"require(?:_relative)?\s+['\"]([^'\"]+)['\"]", line)
        return _first_group(match)
    if language == "php":
        match = re.match(r"use\s+([\w\\]+)", line)
        return _first_group(match)
    return None


def _extract_symbols(language: str, line: str) -> list[tuple[str, str]]:
    patterns = {
        "python": [
            ("Class", r"^class\s+([A-Za-z_]\w*)"),
            ("Function", r"^(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\("),
        ],
        "javascript": [
            ("Class", r"^class\s+([A-Za-z_$][\w$]*)"),
            ("Function", r"^(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\("),
            ("Function", r"^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\("),
        ],
        "typescript": [
            ("Class", r"^(?:export\s+)?class\s+([A-Za-z_$][\w$]*)"),
            ("Function", r"^(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\("),
            ("Function", r"^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\("),
        ],
        "go": [
            ("Function", r"^func\s+(?:\([^)]+\)\s*)?([A-Za-z_]\w*)\s*\("),
            ("Class", r"^type\s+([A-Za-z_]\w*)\s+struct\b"),
        ],
        "java": [
            ("Class", r"^(?:public\s+|private\s+|protected\s+)?(?:final\s+)?class\s+([A-Za-z_]\w*)"),
            ("Function", r"^(?:public|private|protected)\s+[\w<>\[\]]+\s+([A-Za-z_]\w*)\s*\("),
        ],
        "rust": [
            ("Class", r"^(?:pub\s+)?(?:struct|enum|trait)\s+([A-Za-z_]\w*)"),
            ("Function", r"^(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)"),
        ],
        "c": [
            ("Class", r"^(?:typedef\s+)?struct\s+([A-Za-z_]\w*)"),
            ("Function", r"^(?:static\s+|inline\s+)*[\w*]+\s+\**([A-Za-z_]\w*)\s*\([^;]*$"),
        ],
        "cpp": [
            ("Class", r"^(?:class|struct)\s+([A-Za-z_]\w*)"),
            ("Function", r"^(?:static\s+|inline\s+|virtual\s+)*[\w:<>*&]+\s+\**(?:\w+::)?([A-Za-z_]\w*)\s*\([^;]*$"),
        ],
        "ruby": [
            ("Class", r"^(?:class|module)\s+([A-Z]\w*)"),
            ("Function", r"^def\s+(?:self\.)?([A-Za-z_]\w*[?!]?)"),
        ],
        "php": [
            ("Class", r"^(?:abstract\s+|final\s+)?class\s+([A-Za-z_]\w*)"),
            ("Function", r"^(?:public\s+|private\s+|protected\s+|static\s+)*function\s+([A-Za-z_]\w*)\s*\("),
        ],
    }
    found: list[tuple[str, str]] = []
    for kind, pattern in patterns.get(language, []):
        match = re.match(pattern, line)
        if match:
            found.append((kind, match.group(1)))
    return found


def _extract_calls(language: str, line: str) -> list[str]:
    if language not in REGEX_LANGUAGES and language != "javascript":
        return []
    ignored = {"if", "for", "while", "switch", "return", "class", "def", "function", "catch", "new"}
    calls = []
    for match in re.finditer(r"\b([A-Za-z_][\w$]*)\s*\(", line):
        name = match.group(1)
        if name not in ignored and not name[0].isupper():
            calls.append(name)
    return calls


def _dedupe_imports(items: list[ParsedImport]) -> list[ParsedImport]:
    seen: set[str] = set()
    result = []
    for item in items:
        if item.name and item.name not in seen:
            seen.add(item.name)
            result.append(item)
    return result


def _dedupe_calls(calls: list[ParsedCall]) -> list[ParsedCall]:
    seen: set[tuple[str, str, int]] = set()
    result = []
    for call in calls:
        key = (call.caller, call.callee, call.line)
        if key not in seen:
            seen.add(key)
            result.append(call)
    return result


def _dedupe_bindings(items: list[ParsedBinding]) -> list[ParsedBinding]:
    return list(dict.fromkeys(items))


def _first_group(match: re.Match[str] | None) -> str | None:
    if not match:
        return None
    return next((group for group in match.groups() if group), None)
