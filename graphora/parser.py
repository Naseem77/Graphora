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
    Language = None  # type: ignore[assignment,misc]
    Parser = None  # type: ignore[assignment,misc]


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

_TEST_PATH_PATTERN = re.compile(
    r"(^|/)(tests?|__tests__|spec|e2e|cypress)(/|_)|(_test\.|\.test\.|\.spec\.|^test_|/test_)"
)


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
    returns: str = ""  # declared return type name, when the source states one
    is_test: bool = False  # test code inside a non-test file (Rust #[cfg(test)] / #[test])
    attributes: str = ""  # classes: "field=Type;other=Type" for fields whose type is stated
    bases: str = ""  # classes: comma-separated base classes as written ("Base,mixins.Mixin")

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
    scope: str = ""  # qualified name of the function the import is in ("" = module level)


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


# Manifests that say which imports are local: Go's module path, Rust's crate name.
MANIFESTS = {"go.mod": "gomod", "Cargo.toml": "cargo"}


def is_supported_code_file(path: str) -> bool:
    return Path(path).suffix.lower() in CODE_EXTENSIONS or Path(path).name in MANIFESTS


def is_test_path(path: str) -> bool:
    return bool(_TEST_PATH_PATTERN.search(path.replace("\\", "/").lower()))


def language_for_path(path: str) -> str:
    if Path(path).name in MANIFESTS:
        return MANIFESTS[Path(path).name]
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
    if language == "gomod":
        return _parse_go_mod(path, content)
    if language == "cargo":
        return _parse_cargo_toml(path, content)
    parsed = _parse_with_tree_sitter(path, content, language)
    if parsed is not None:
        return parsed
    return _parse_with_regex(path, content, language)


def _parse_go_mod(path: str, content: str) -> ParsedFile:
    """go.mod declares the module path that makes an import local to the repository."""
    match = re.search(r"(?m)^\s*module\s+(\S+)", content)
    bindings = [ParsedBinding("<module>", match.group(1).strip('"'))] if match else []
    return ParsedFile(path, "gomod", [], [], [], content, parser_used="regex", bindings=bindings)


def _parse_cargo_toml(path: str, content: str) -> ParsedFile:
    """Cargo.toml names the crate: `use my_crate::x` inside the repository is local."""
    name = ""
    section = ""
    for raw in content.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line.startswith("["):
            section = line.strip("[] ")
            continue
        match = re.match(r'name\s*=\s*"([^"]+)"', line)
        if match and section in {"package", "lib"}:
            name = match.group(1)  # [lib] name, when given, wins over [package]
    bindings = [ParsedBinding("<crate>", name.replace("-", "_"))] if name else []
    return ParsedFile(path, "cargo", [], [], [], content, parser_used="regex", bindings=bindings)


@dataclass(frozen=True)
class DiffFile:
    """One file of a unified diff."""

    path: str  # new path, or the old path when the file was deleted
    deleted: bool
    added: list[tuple[int, str]]  # (new-side line, text)
    removed: list[tuple[int, int, str]]  # (old-side line, new-side line it sits before, text)


_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_unified_diff(diff: str) -> list[DiffFile]:
    """Parse git and GNU unified diffs.

    Hunk line counts are tracked, so a removed line whose text starts with
    "--" (raw "--- ...") is content, not a file header; GNU timestamps after
    a tab and git's quoted paths ("b/sp\\303\\244ce.py") are handled.
    """
    files: list[DiffFile] = []
    old_path = new_path = ""
    added: list[tuple[int, str]] = []
    removed: list[tuple[int, int, str]] = []
    old_left = new_left = 0
    old_line = new_line = 0

    def flush() -> None:
        if new_path or old_path:
            deleted = new_path in ("", "/dev/null")
            files.append(DiffFile(old_path if deleted else new_path, deleted, list(added), list(removed)))

    lines = diff.splitlines()
    for index, raw in enumerate(lines):
        if (old_left > 0 or new_left > 0) and _starts_file_header(lines, index):
            old_left = new_left = 0  # hand-written hunk with wrong counts: a new file starts here
        if old_left > 0 or new_left > 0:  # inside a hunk
            if raw.startswith("\\"):
                continue  # "\ No newline at end of file"
            if raw.startswith("+"):
                added.append((new_line, raw[1:]))
                new_line += 1
                new_left -= 1
            elif raw.startswith("-"):
                removed.append((old_line, new_line, raw[1:]))
                old_line += 1
                old_left -= 1
            else:  # context (" text", or "" for an empty context line)
                old_line += 1
                new_line += 1
                old_left -= 1
                new_left -= 1
            continue
        if raw.startswith("--- "):
            flush()
            old_path, new_path = _diff_path(raw[4:]), ""
            added, removed = [], []
        elif raw.startswith("+++ "):
            new_path = _diff_path(raw[4:])
        else:
            match = _HUNK_HEADER.match(raw)
            if match:
                old_line, new_line = int(match.group(1)), int(match.group(3))
                old_left = int(match.group(2)) if match.group(2) is not None else 1
                new_left = int(match.group(4)) if match.group(4) is not None else 1
    flush()
    return files


def _starts_file_header(lines: list[str], index: int) -> bool:
    return (
        lines[index].startswith("--- ")
        and index + 2 < len(lines)
        and lines[index + 1].startswith("+++ ")
        and lines[index + 2].startswith("@@ ")
    )


def _diff_path(text: str) -> str:
    text = text.rstrip("\n").split("\t", 1)[0].strip()  # GNU diff: "path<TAB>timestamp"
    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape")
        text = text.encode("latin-1").decode("utf-8", "replace")
    if text == "/dev/null":
        return text
    return text[2:] if text.startswith(("a/", "b/")) else text


def definition_names(path: str, texts: list[str]) -> list[str]:
    """Names of symbols defined on the given lines of `path` (its language's patterns only)."""
    language = language_for_path(path)
    languages: tuple[str, ...] = ("typescript",) if language == "javascript" else (language,)
    if language == "text":
        languages = REGEX_LANGUAGES
    names: list[str] = []
    for text in texts:
        for lang in languages:
            names.extend(name for _, name in _extract_symbols(lang, text.strip()))
    return list(dict.fromkeys(names))


def extract_symbols_from_diff(diff: str) -> list[str]:
    """Names of symbols defined on added/removed diff lines.

    Each line is matched only with the definition patterns of its file's
    language; C's patterns used to read Python's `return len(` as a
    definition of `len`.
    """
    names: list[str] = []
    for f in parse_unified_diff(diff):
        names.extend(definition_names(f.path, [t for _, _, t in f.removed] + [t for _, t in f.added]))
    return list(dict.fromkeys(names))


def extract_files_from_diff(diff: str) -> list[str]:
    """Paths of the files a unified diff changes or adds (not those it deletes)."""
    return list(dict.fromkeys(f.path for f in parse_unified_diff(diff) if not f.deleted))


# --- tree-sitter path -------------------------------------------------------

# Values that make a variable, property, or class field a function definition.
_JS_FUNCTION_VALUES = {"arrow_function", "function_expression", "function", "generator_function"}
# Identifiers in any script (Ñ, π), with JS `$`/`#private`, C++ `~Dtor`, Ruby `paid?`/`name=`.
_NAME_PATTERN = re.compile(r"^(?:[~#$]?[^\W\d][\w$]*[?!=]?|\$+)$")
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
    test: bool = False


def _parse_with_tree_sitter(path: str, content: str, language: str) -> ParsedFile | None:
    grammar = "tsx" if path.lower().endswith(".tsx") else language
    parser = _tree_sitter_parser(grammar)
    if parser is None:
        return None

    content_bytes = content.encode("utf-8", errors="replace")  # lone surrogates must not crash
    root = parser.parse(content_bytes).root_node
    symbols: list[ParsedSymbol] = []
    imports: list[ParsedImport] = []
    bindings: list[ParsedBinding] = []
    calls: list[ParsedCall] = []

    # Variable -> type facts, keyed by the scope that owns them ("" = module).
    types: dict[str, dict[str, str]] = {}
    # Ruby local variable names per method: a bare identifier that is not one is a call.
    ruby_locals: dict[str, set[str]] = {}

    # Iterative pre-order walk carrying the enclosing scopes (deep trees overflow recursion).
    stack: list[tuple[Any, tuple[_Frame, ...]]] = [(root, ())]
    while stack:
        node, scope = stack.pop()
        child_scope = scope

        for target, kind in _tree_sitter_types(language, node, content_bytes):
            if scope and scope[-1].kind == "Class" and not target.startswith(_SELF_PREFIXES):
                if language in {"python", "javascript", "typescript"}:
                    # A class-body annotation (`evidence: Ledger`, dataclass fields) types the attribute.
                    target = ("self." if language == "python" else "this.") + target
                else:
                    # Java & co. use fields without `this.`: keep the bare name on the class.
                    types.setdefault(scope[-1].qualname, {})[target] = kind
                    continue
            owner = _type_owner(scope, target)
            types.setdefault(owner, {})[target] = kind

        imported = _tree_sitter_import(language, node, content_bytes)
        if imported:
            imports.append(ParsedImport(name=imported, confidence=EXTRACTED))
        found_bindings = _tree_sitter_bindings(language, node, content_bytes)
        if found_bindings:
            owner = next((f.qualname for f in reversed(scope) if f.kind == "Function"), "")
            bindings.extend(replace(b, scope=owner) for b in found_bindings)

        definition = _tree_sitter_definition(language, node, content_bytes)
        if definition is not None:
            parent = scope[-1].qualname if scope else ""
            parent_kind = scope[-1].kind if scope else ""
            if definition.qualifiers:
                parent = ".".join(part for part in (parent, *definition.qualifiers) if part)
                parent_kind = definition.qualifier_kind
            qualname = f"{parent}.{definition.name}" if parent else definition.name
            returns = _return_type(language, node, content_bytes) if definition.kind == "Function" else ""
            if returns in {"Self", "self", "this", "static"} and parent_kind == "Class":
                returns = parent.rsplit(".", 1)[-1]
            in_test = any(f.test for f in scope) or _is_test_item(language, node, definition.name, content_bytes)
            bases = ",".join(_base_classes(language, node, content_bytes)) if definition.kind == "Class" else ""
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
                        returns=returns,
                        is_test=in_test,
                        bases=bases,
                    )
                )
            child_scope = scope + (_Frame(qualname, definition.kind, in_test),)

        call = _tree_sitter_call(language, node, content_bytes)
        if call is not None:
            callee, receiver = call
            receiver = _typed_receiver(receiver, scope, types)
            caller = next((f.qualname for f in reversed(scope) if f.kind == "Function"), None)
            # Self-recursion adds no impact information; drop it as before.
            if caller and not (receiver == "" and caller.rsplit(".", 1)[-1] == callee):
                line = _point_row(node.start_point) + 1
                calls.append(ParsedCall(caller=caller, callee=callee, line=line, confidence=INFERRED, receiver=receiver))

        if language == "ruby":
            bare = _ruby_bare_call(node, scope, ruby_locals, content_bytes)
            if bare is not None:
                calls.append(bare)

        stack.extend((child, child_scope) for child in reversed(node.children))

    symbols = _settle_qualifier_kinds(_with_attributes(symbols, types))

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


# Nodes holding a class's base classes, per grammar (implements/interfaces are not bases:
# interface methods are signatures, not the code that runs).
_BASE_NODES = {"class_heritage", "extends_clause", "superclass", "base_clause", "base_class_clause"}


def _base_classes(language: str, node: Any, content: bytes) -> list[str]:
    """Base classes named in a class definition: Foo(Base), extends B, < B, : public B."""
    found: list[str] = []
    if language == "python":
        supers = node.child_by_field_name("superclasses")
        for child in supers.named_children if supers is not None else []:
            if child.type in {"identifier", "attribute"}:
                found.append(_node_text(child, content))
        return found
    stack = [c for c in node.children if c.type in _BASE_NODES]
    while stack:
        current = stack.pop(0)
        if current.type == "extends_clause" or current.type in _BASE_NODES:
            for child in current.named_children:
                if child.type in {"implements_clause", "class_interface_clause", "access_specifier"}:
                    continue
                if child.type in _BASE_NODES:
                    stack.append(child)
                elif child.type in {
                    "identifier", "type_identifier", "constant", "name", "qualified_name",
                    "scope_resolution", "member_expression", "qualified_identifier", "generic_type",
                }:
                    found.append(_node_text(child, content).split("<")[0].strip())
    return [name for name in found if name]


def _is_test_item(language: str, node: Any, name: str, content: bytes) -> bool:
    """Rust inline tests: `#[cfg(test)] mod tests`, `#[test]`/`#[tokio::test]` functions."""
    if language in {"javascript", "typescript"} and node.type == "call_expression":
        return True  # a describe/it/test block (only those become definitions)
    if language != "rust" or node.type not in {"mod_item", "function_item"}:
        return False
    if node.type == "mod_item" and name == "tests":
        return True
    sibling = node.prev_named_sibling
    while sibling is not None and sibling.type == "attribute_item":
        text = _node_text(sibling, content)
        if "cfg(test)" in text or re.search(r"\btest\b", text):
            return True
        sibling = sibling.prev_named_sibling
    return False


_RUBY_BARE_CALL_PARENTS = {
    "body_statement", "then", "else", "begin", "argument_list", "assignment", "binary", "return",
    "parenthesized_statements", "if", "unless", "while", "until", "conditional", "array", "pair",
    "interpolation", "do_block", "block_body", "ensure", "rescue",
}


def _ruby_bare_call(
    node: Any, scope: tuple[_Frame, ...], locals_by_method: dict[str, set[str]], content: bytes
) -> ParsedCall | None:
    """Ruby calls a method without parentheses: `helper` alone is a call unless it is a local."""
    method = next((f.qualname for f in reversed(scope) if f.kind == "Function"), None)
    if method is None:
        return None
    known = locals_by_method.setdefault(method, set())
    parent = node.parent
    if node.type == "assignment":
        left = node.child_by_field_name("left")
        if left is not None and left.type == "identifier":
            known.add(_node_text(left, content))
        return None
    if node.type in {"method_parameters", "block_parameters", "lambda_parameters"}:
        for child in node.named_children:
            name_node = child if child.type == "identifier" else child.child_by_field_name("name")
            if name_node is not None:
                known.add(_node_text(name_node, content))
        return None
    if node.type != "identifier" or parent is None or parent.type not in _RUBY_BARE_CALL_PARENTS:
        return None
    if parent.type == "assignment" and parent.child_by_field_name("left") == node:
        return None
    name = _node_text(node, content)
    if name in known or not _NAME_PATTERN.match(name):
        return None
    return ParsedCall(method, name, _point_row(node.start_point) + 1, INFERRED, "")


def _type_owner(scope: tuple[_Frame, ...], target: str) -> str:
    """`self.x`/`this.x`/`@x` belong to the enclosing class; other names to the innermost function."""
    wanted = "Class" if target.startswith(_SELF_PREFIXES) else "Function"
    for frame in reversed(scope):
        if frame.kind == wanted:
            return frame.qualname
    return ""


def _typed_receiver(receiver: str, scope: tuple[_Frame, ...], types: dict[str, dict[str, str]]) -> str:
    """Replace a variable receiver with its known type (or the call that produced it)."""
    if not receiver or receiver == "<expr>" or not types:
        return receiver
    owners = [*(frame.qualname for frame in reversed(scope)), ""]

    def lookup(key: str) -> str:
        key = _normal_target(key)
        for owner in owners:
            scope_types = types.get(owner, {})
            if key in scope_types:
                return scope_types[key]  # "" = shadowed by an untyped local: unknown
        return ""

    for _ in range(4):  # `g = db.select_graph()` where `db = FalkorDB()`: follow the chain
        if receiver.endswith("()"):
            head = receiver[:-2]
            first = re.split(r"\.|::|->", head, maxsplit=1)[0]
            typed = lookup(first) if first != head else ""
            if not typed:
                return receiver
            receiver = typed.removesuffix("()") + head[len(first):] + "()" if not typed.endswith("()") else (
                typed[:-2] + "()." + head[len(first) + 1 :] + "()"
            )
            if not _CALL_RESULT.match(receiver):
                return "<expr>"
            continue
        typed = lookup(receiver)
        if typed:
            receiver = typed
            continue
        # `state.evidence` where `state: AgentState`: the type, then the field chain
        # ("AgentState#evidence"); the resolver reads the field types of the class.
        parts = receiver.split(".")
        for cut in range(len(parts) - 1, 0, -1):
            typed = lookup(".".join(parts[:cut]))
            if typed and "#" not in typed and "|" not in typed:
                return f"{typed}#{'.'.join(parts[cut:])}"
        return receiver
    return receiver


def _return_type(language: str, node: Any, content: bytes) -> str:
    """The declared return type of a function definition node, if stated."""
    field = node.child_by_field_name
    if language in {"javascript", "typescript"} and node.type in {
        "variable_declarator", "pair", "field_definition", "public_field_definition"
    }:
        value = field("value")
        return _return_type(language, value, content) if value is not None else ""
    if language in {"python", "javascript", "typescript", "rust", "php"}:
        return _type_name(_node_text(field("return_type"), content), returned=True)
    if language == "go":
        result = field("result")
        if result is None or result.type == "parameter_list":
            return ""
        return _type_name(_node_text(result, content), returned=True)
    if language in {"java", "c", "cpp"}:
        return _type_name(_node_text(field("type"), content), returned=True)
    return ""


def _with_attributes(symbols: list[ParsedSymbol], types: dict[str, dict[str, str]]) -> list[ParsedSymbol]:
    """Record each class's typed fields so `obj.field.method()` can be resolved."""
    out = []
    for s in symbols:
        if s.kind == "Class" and types.get(s.qualname):
            fields = sorted(
                f"{target.split('.', 1)[-1].lstrip('@')}={kind}"
                for target, kind in types[s.qualname].items()
                if kind and target.startswith(_SELF_PREFIXES) and target.count(".") <= 1 and "#" not in kind
            )
            if fields:
                s = replace(s, attributes=";".join(fields))
        out.append(s)
    return out


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
    if not module_name or not language_attr:
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


_JS_TEST_BLOCKS = {"describe", "it", "test", "context", "suite", "specify",
                   "beforeEach", "afterEach", "beforeAll", "afterAll", "before", "after"}


def _js_test_block(node: Any, content: bytes) -> _Definition | None:
    """`it("does x", () => {...})` and friends: a test symbol named after its label,
    so the calls inside the callback count as test callers."""
    function = node.child_by_field_name("function")
    if function is None:
        return None
    base = ""
    if function.type == "identifier":
        base = _node_text(function, content)
    elif function.type == "member_expression":  # test.only(...), it.skip(...), test.describe(...)
        obj = _node_text(function.child_by_field_name("object"), content)
        prop = _node_text(function.child_by_field_name("property"), content)
        base = prop if prop in {"describe", "step"} else obj
    if base not in _JS_TEST_BLOCKS and base != "step":
        return None
    args = node.child_by_field_name("arguments")
    values = args.named_children if args is not None else []
    if not any(v.type in _JS_FUNCTION_VALUES for v in values):
        return None
    label = next((_node_text(v, content).strip("\"'`") for v in values if v.type in {"string", "template_string"}), "")
    slug = re.sub(r"\W+", "_", label).strip("_")[:60]
    return _Definition("Function", f"{base}_{slug}" if slug else base)


def _is_default_export(node: Any) -> bool:
    parent = node.parent
    return parent is not None and parent.type == "export_statement" and any(c.type == "default" for c in parent.children)


def _js_definition(node: Any, content: bytes) -> _Definition | None:
    kind = node.type
    if kind == "call_expression":
        return _js_test_block(node, content)
    if kind in {"function_declaration", "generator_function_declaration", "method_definition"}:
        return _named("Function", node, content)
    if kind in _JS_FUNCTION_VALUES and node.child_by_field_name("name") is None and _is_default_export(node):
        return _Definition("Function", "default")  # export default function () {}
    if kind == "assignment_expression":
        return _js_exports_assignment(node, content)
    if kind in {"class_declaration", "abstract_class_declaration", "class", "interface_declaration", "enum_declaration"}:
        named = _named("Class", node, content)
        if named is not None or node.child_by_field_name("name") is not None:
            return named
        if _is_default_export(node):
            return _Definition("Class", "default")  # export default class {}
        if node.parent is not None and node.parent.type == "variable_declarator":
            return None  # `const K = class {}` is named by the declarator
        # An anonymous class still scopes its methods (they are not top-level functions).
        return _Definition("Class", f"anonymous_{_point_row(node.start_point) + 1}", emit=False)
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
        if value.type == "class":
            return _named("Class", node, content)  # const K = class { ... }
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


def _js_exports_assignment(node: Any, content: bytes) -> _Definition | None:
    """CommonJS: `module.exports = function x() {}`, `exports.foo = function () {}`."""
    left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
    if left is None or right is None or left.type != "member_expression" or right.type not in _JS_FUNCTION_VALUES:
        return None
    target = _node_text(left, content).replace(" ", "")
    if target == "module.exports":
        own = _node_text(right.child_by_field_name("name"), content)
        return _Definition("Function", own or "default")
    if target.startswith(("exports.", "module.exports.")):
        name = target.rsplit(".", 1)[-1]
        return _Definition("Function", name) if _NAME_PATTERN.match(name) else None
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
        # `impl<'a> Tr for &'a mut Foo<i32>`: the type is Foo.
        type_text = re.sub(r"^&\s*(?:'\w+\s+)?(?:mut\s+)?(?:dyn\s+)?", "", type_text.strip())
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
        if name_node is not None and name_node.type == "template_function":  # template<> int f<int>(...)
            name_node = name_node.child_by_field_name("name")
        name = _node_text(name_node, content).strip()
        if name_node is not None and name_node.type == "operator_name":
            name = "".join(name.split())  # operator+, operator==
        elif not _NAME_PATTERN.match(name):
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
    if node.type == "anonymous_class":  # new class { ... }: its methods are not top-level functions
        return _Definition("Class", f"anonymous_{_point_row(node.start_point) + 1}", emit=False)
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
        if declarator.type in {
            "identifier", "field_identifier", "destructor_name", "qualified_identifier",
            "operator_name", "template_function",
        }:
            return declarator
        if declarator.type == "parenthesized_declarator":  # int (*f(void))(int): f returns a pointer
            declarator = declarator.named_children[0] if declarator.named_children else None
            depth += 1
            continue
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
        return _split_callee(language, field("function"), content, ("attribute", "object", "attribute"))
    if language in {"javascript", "typescript"}:
        if t == "call_expression":
            return _split_callee(language, field("function"), content, ("member_expression", "object", "property"))
        if t == "new_expression":
            return _bare(_node_text(field("constructor"), content))
        return None
    if language == "go" and t == "call_expression":
        return _split_callee(language, field("function"), content, ("selector_expression", "operand", "field"))
    if language == "java":
        if t == "method_invocation":
            return _with_receiver(_node_text(field("name"), content), _receiver_text(language, field("object"), content))
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
        return _split_callee(language, function, content, ("field_expression", "value", "field"))
    if language in {"c", "cpp"} and t == "call_expression":
        function = field("function")
        if function is not None and function.type == "qualified_identifier":
            return _with_receiver(
                _node_text(_innermost_name(function), content),
                _node_text(function.child_by_field_name("scope"), content),
            )
        return _split_callee(language, function, content, ("field_expression", "argument", "field"))
    if language == "ruby" and t == "call":
        method = _node_text(field("method"), content)
        receiver = _receiver_text(language, field("receiver"), content)
        if method in {"require", "require_relative"}:
            return None
        if method == "new":
            # Invoice.new(...) constructs Invoice; a bare `new` is Class.new on self.
            return _bare(receiver.split("::")[-1]) if receiver else None
        return _with_receiver(method, receiver)
    if language == "php":
        if t == "function_call_expression":
            function = field("function")
            if function is None or function.type == "variable_name":
                return None  # `$helper()` calls whatever the variable holds
            return _bare(_node_text(function, content).split("\\")[-1])
        if t == "member_call_expression":  # $this->log(), $obj->save(): drop PHP's `$` sigil
            receiver = _receiver_text(language, field("object"), content)
            return _with_receiver(_node_text(field("name"), content), receiver.lstrip("$").replace("->$", "->"))
        if t == "scoped_call_expression":
            return _with_receiver(_node_text(field("name"), content), _node_text(field("scope"), content))
        if t == "object_creation_expression":
            for child in node.named_children:
                if child.type in {"name", "qualified_name"}:
                    return _bare(_node_text(child, content).split("\\")[-1])
        return None
    return None


def _split_callee(
    language: str, function: Any | None, content: bytes, member: tuple[str, str, str]
) -> tuple[str, str] | None:
    if function is None:
        return None
    member_type, object_field, name_field = member
    if function.type == member_type:
        return _with_receiver(
            _node_text(function.child_by_field_name(name_field), content),
            _receiver_text(language, function.child_by_field_name(object_field), content),
        )
    if function.type in {"identifier", "name"}:
        return _bare(_node_text(function, content))
    return None


_CALL_NODES = {
    "call", "call_expression", "method_invocation", "member_call_expression",
    "scoped_call_expression", "function_call_expression",
}
# Receiver that is the result of a call: "db.select_graph()" (resolved via the callee's return type).
_CALL_RESULT = re.compile(r"^(?:[A-Za-z_]\w*(?:\(\))?(?:\.|::|->))*[A-Za-z_]\w*\(\)$")


def _receiver_text(language: str, node: Any | None, content: bytes, depth: int = 0) -> str:
    """Receiver text; a call used as a receiver becomes `inner.callee()`."""
    if node is None:
        return ""
    if node.type in {"await", "await_expression", "parenthesized_expression"} and node.named_children:
        return _receiver_text(language, node.named_children[-1], content, depth)
    if node.type in _CALL_NODES and depth < 8:
        text = _node_text(node, content)
        if _CONSTRUCTED_RECEIVER.match(" ".join(text.split())):
            return text  # `Db()`: _with_receiver turns it into the type
        nested = _tree_sitter_call(language, node, content)
        if nested is not None and nested[1] != "<expr>":
            name, receiver = nested
            return f"{receiver}.{name}()" if receiver else f"{name}()"
        return "<expr>"
    return _node_text(node, content)


# A capitalized constructor call used as a receiver; the name says its type.
_CONSTRUCTED_RECEIVER = re.compile(r"^(?:new\s+)?((?:[A-Za-z_]\w*(?:\.|::))*[A-Z]\w*)\s*\([^()]*\)$")


def _bare(name: str) -> tuple[str, str] | None:
    return _with_receiver(name, "")


def _with_receiver(name: str, receiver: str) -> tuple[str, str] | None:
    name = name.strip().lstrip("&")
    if not _NAME_PATTERN.match(name):
        return None
    receiver = " ".join(receiver.split())
    constructed = _CONSTRUCTED_RECEIVER.match(receiver)
    if constructed:
        receiver = constructed.group(1)  # `Db().save()` / `new Db().save()`: the receiver is a Db
    if _CALL_RESULT.match(receiver):
        return name, receiver
    if any(ch in receiver for ch in "()[]{}\"'") or len(receiver) > 120:
        receiver = "<expr>"  # call chains, subscripts: receiver type is unknown
    return name, receiver


# --- local type facts ---------------------------------------------------------
#
# A light, flow-insensitive type pass: when the source states a variable's
# type (a typed parameter or annotation) or assigns it a constructor call
# (`db = FalkorDB()`, `x := &Foo{}`, `self.c = Client()`), calls on that
# variable are recorded with the type as their receiver, so `db.select_graph()`
# resolves to FalkorDB.select_graph instead of every method of that name.

_TYPE_NAME = re.compile(r"^[^\W\d]\w*(?:(?:\.|::|\\)[^\W\d]\w*)*$")
_SELF_PREFIXES = ("self.", "this.", "@")


# Wrappers whose methods are the inner type's: Optional (a union with None), and
# Rust smart pointers (auto-deref). Option/Result/Future have methods of their own.
_TRANSPARENT_WRAPPERS = ("Optional[", "Box<", "Rc<", "Arc<")
# Unwrapped only as a return type, where callers `await` the value.
_AWAITED_WRAPPERS = ("Awaitable[", "Promise<")


def _type_name(text: str, returned: bool = False) -> str:
    text = " ".join(text.split()).lstrip(":").strip().strip("\"'")
    for prefix in ("&mut ", "&", "*", "?", "mut "):
        text = text.removeprefix(prefix).strip()
    wrappers = _TRANSPARENT_WRAPPERS + (_AWAITED_WRAPPERS if returned else ())
    for wrapper in wrappers:
        if text.startswith(wrapper) and text.endswith(("]", ">")):
            text = text[len(wrapper) : -1].split(",")[0].strip().strip("\"'")
    if "|" in text or text.startswith("Union["):
        inner = text[len("Union[") : -1] if text.startswith("Union[") else text
        parts = [p.strip() for p in re.split(r"[|,]", inner) if p.strip() not in ("None", "null", "undefined")]
        names = [_type_name(p) for p in parts]
        return "|".join(names) if names and all(names) else ""
    text = text.split("<")[0].strip()
    return text if _TYPE_NAME.match(text) else ""


def _constructed_type(language: str, node: Any | None, content: bytes) -> str:
    """The type a constructor-like expression produces, or ""."""
    if node is None:
        return ""
    t = node.type
    if language == "python" and t == "call":
        name = _node_text(node.child_by_field_name("function"), content)
        return name if name.rsplit(".", 1)[-1][:1].isupper() and _TYPE_NAME.match(name) else ""
    if language in {"javascript", "typescript"} and t == "new_expression":
        return _type_name(_node_text(node.child_by_field_name("constructor"), content))
    if language == "java" and t == "object_creation_expression":
        return _type_name(_node_text(node.child_by_field_name("type"), content))
    if language == "go":
        if t == "unary_expression":
            return _constructed_type(language, node.child_by_field_name("operand"), content)
        if t == "composite_literal":  # Foo{} and generic Foo[int]{}
            return _type_name(_node_text(node.child_by_field_name("type"), content).split("[")[0])
    if language == "rust":
        if t == "struct_expression":
            return _type_name(_node_text(node.child_by_field_name("name"), content))
        if t == "call_expression":
            function = node.child_by_field_name("function")
            if function is not None and function.type == "scoped_identifier":
                name = _node_text(function.child_by_field_name("name"), content)
                if name in {"new", "default", "from", "with_capacity", "builder", "open", "connect"}:
                    return _type_name(_node_text(function.child_by_field_name("path"), content))
    if language == "php" and t == "object_creation_expression":
        for child in node.named_children:
            if child.type in {"name", "qualified_name"}:
                return _type_name(_node_text(child, content))
    if language == "ruby" and t == "call" and _node_text(node.child_by_field_name("method"), content) == "new":
        return _type_name(_node_text(node.child_by_field_name("receiver"), content))
    return ""


def _assigned_type(language: str, node: Any | None, content: bytes) -> str:
    """Constructor type, or `recv.callee()` for a value returned by a call."""
    constructed = _constructed_type(language, node, content)
    if constructed or node is None:
        return constructed
    if node.type in {"await", "await_expression"} and node.named_children:
        return _assigned_type(language, node.named_children[-1], content)
    if node.type in _CALL_NODES:
        result = _receiver_text(language, node, content)
        return result if _CALL_RESULT.match(result) else ""
    return ""


def _normal_target(text: str) -> str:
    text = text.strip().lstrip("$").replace("->", ".")
    return text


def _tree_sitter_types(language: str, node: Any, content: bytes) -> list[tuple[str, str]]:
    """(variable, type) facts stated by this node."""
    t = node.type
    field = node.child_by_field_name

    def text(n: Any | None) -> str:
        return _node_text(n, content)

    out: list[tuple[str, str]] = []
    if language == "python":
        if t == "parameters":
            for child in node.named_children:  # untyped parameters shadow outer names
                if child.type == "identifier":
                    out.append((text(child), ""))
                elif child.type == "default_parameter":
                    out.append((text(child.child_by_field_name("name")), ""))
        elif t == "for_statement" and field("left") is not None and field("left").type == "identifier":
            out.append((text(field("left")), ""))
        elif t == "typed_parameter":
            names = [c for c in node.named_children if c.type == "identifier"]
            if names:
                out.append((text(names[0]), _type_name(text(field("type")))))
        elif t == "typed_default_parameter":
            out.append((text(field("name")), _type_name(text(field("type")))))
        elif t == "assignment":
            left = field("left")
            if left is not None and left.type in {"identifier", "attribute"}:
                declared = _type_name(text(field("type"))) if field("type") is not None else ""
                out.append((text(left), declared or _assigned_type(language, field("right"), content)))
    elif language in {"javascript", "typescript"}:
        if t == "formal_parameters":
            out.extend((text(c), "") for c in node.named_children if c.type == "identifier")
        elif t in {"for_in_statement"} and field("left") is not None:
            left = field("left")
            names = [left] if left.type == "identifier" else [c for c in left.named_children if c.type == "identifier"]
            out.extend((text(n), "") for n in names)
        elif t in {"required_parameter", "optional_parameter"}:
            pattern = field("pattern")
            if pattern is not None and pattern.type == "identifier":
                out.append((text(pattern), _type_name(text(field("type")))))
        elif t == "variable_declarator":
            name = field("name")
            if name is not None and name.type == "identifier":
                declared = _type_name(text(field("type"))) if field("type") is not None else ""
                out.append((text(name), declared or _assigned_type(language, field("value"), content)))
        elif t == "assignment_expression":
            out.append((text(field("left")), _assigned_type(language, field("right"), content)))
        elif t == "public_field_definition" and field("type") is not None:
            out.append((f"this.{text(field('name'))}", _type_name(text(field("type")))))
    elif language == "java":
        if t == "enhanced_for_statement":
            out.append((text(field("name")), _type_name(text(field("type")))))
        elif t == "formal_parameter":
            out.append((text(field("name")), _type_name(text(field("type")))))
        elif t in {"local_variable_declaration", "field_declaration"}:
            declared = _type_name(text(field("type")))
            for declarator in _field_children(node, "declarator"):
                name = text(declarator.child_by_field_name("name"))
                kind = declared if declared != "var" else _assigned_type(
                    language, declarator.child_by_field_name("value"), content
                )
                out.append((f"this.{name}" if t == "field_declaration" else name, kind))
                if t == "field_declaration":
                    out.append((name, kind))  # Java code calls fields without `this.`
    elif language == "go":
        if t in {"parameter_declaration", "var_spec"}:
            kind = _type_name(text(field("type")))
            out.extend((text(name), kind) for name in _field_children(node, "name"))
        elif t == "short_var_declaration":
            lefts = field("left").named_children if field("left") is not None else []
            rights = field("right").named_children if field("right") is not None else []
            for left, right in zip(lefts, rights):
                out.append((text(left), _assigned_type(language, right, content)))
    elif language == "rust":
        if t == "parameter":
            out.append((text(field("pattern")), _type_name(text(field("type")))))
        elif t == "let_declaration":
            declared = _type_name(text(field("type"))) if field("type") is not None else ""
            out.append((text(field("pattern")), declared or _assigned_type(language, field("value"), content)))
    elif language == "php":
        if t == "simple_parameter":
            out.append((text(field("name")), _type_name(text(field("type")))))
        elif t == "assignment_expression":
            out.append((text(field("left")), _assigned_type(language, field("right"), content)))
    elif language == "ruby" and t == "assignment":
        out.append((text(field("left")), _assigned_type(language, field("right"), content)))
    # An empty type is kept for plain names: it shadows an outer variable of the same
    # name (a parameter or loop variable is not the module-level `x = A()`). A
    # self./this. attribute keeps whatever type another method gave it.
    return [
        (_normal_target(target), kind)
        for target, kind in out
        if target and (kind or not target.startswith(_SELF_PREFIXES))
    ]


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
    if node.type == "export_statement" and node.child_by_field_name("source") is not None:
        # Re-exports: `export { a as b } from "./x"`, `export * from "./x"`.
        source = _node_text(node.child_by_field_name("source"), content).strip("\"'`")
        clause = next((c for c in node.named_children if c.type == "export_clause"), None)
        if clause is None:
            return [ParsedBinding("*", source, "*")]
        out = []
        for spec in clause.named_children:
            if spec.type == "export_specifier":
                name = _node_text(spec.child_by_field_name("name"), content)
                alias = _node_text(spec.child_by_field_name("alias"), content) or name
                out.append(ParsedBinding(alias, source, name))
        return out
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


def _rust_join(prefix: str, path: str) -> str:
    return f"{prefix}::{path}" if prefix else path


def _rust_use_tree(node: Any | None, prefix: str, content: bytes, out: list[ParsedBinding]) -> None:
    if node is None:
        return
    if node.type in {"identifier", "self", "crate", "super"}:
        name = _node_text(node, content)
        out.append(ParsedBinding(name, prefix, name) if prefix else ParsedBinding(name, name))
    elif node.type == "scoped_identifier":
        path = _node_text(node.child_by_field_name("path"), content)
        name = _node_text(node.child_by_field_name("name"), content)
        out.append(ParsedBinding(name, _rust_join(prefix, path), name))
    elif node.type == "use_as_clause":
        path = _node_text(node.child_by_field_name("path"), content)
        alias = _node_text(node.child_by_field_name("alias"), content)
        owner, _, name = _rust_join(prefix, path).rpartition("::")
        out.append(ParsedBinding(alias, owner, name) if owner else ParsedBinding(alias, name))
    elif node.type == "scoped_use_list":
        path = _node_text(node.child_by_field_name("path"), content)
        for child in (node.child_by_field_name("list").named_children if node.child_by_field_name("list") else []):
            _rust_use_tree(child, _rust_join(prefix, path), content, out)
    elif node.type == "use_list":
        for child in node.named_children:
            _rust_use_tree(child, prefix, content, out)
    elif node.type == "use_wildcard":
        path = _node_text(node, content).removesuffix("::*").removesuffix("*")
        out.append(ParsedBinding("*", _rust_join(prefix, path), "*"))


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
    # The receiver is part of the key: `r.area() + c.area()` on one line is two calls.
    seen: set[tuple[str, str, int, str]] = set()
    result = []
    for call in calls:
        key = (call.caller, call.callee, call.line, call.receiver)
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
