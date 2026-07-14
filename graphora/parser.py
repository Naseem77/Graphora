"""Deterministic source parsing with tree-sitter (regex fallback).

No LLM anywhere in this module. Every extracted fact carries a confidence
tag so downstream consumers can tell what was read directly from source
(EXTRACTED), what was resolved by name matching (INFERRED), and what is
uncertain (AMBIGUOUS).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class ParsedCall:
    caller: str
    callee: str
    line: int
    confidence: str = INFERRED


@dataclass(frozen=True)
class ParsedImport:
    name: str
    confidence: str = EXTRACTED


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
    """Names of top-level symbols that appear on added/removed diff lines."""
    symbols: list[str] = []
    seen: set[str] = set()
    for line in diff.splitlines():
        if line.startswith(("+++", "---")) or not line.startswith(("+", "-")):
            continue
        stripped = line[1:].strip()
        for language in REGEX_LANGUAGES:
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


def _parse_with_tree_sitter(path: str, content: str, language: str) -> ParsedFile | None:
    parser = _tree_sitter_parser(language)
    if parser is None:
        return None

    content_bytes = content.encode("utf-8")
    root = parser.parse(content_bytes).root_node
    symbols: list[ParsedSymbol] = []
    imports: list[ParsedImport] = []
    calls: list[ParsedCall] = []
    function_ranges: list[tuple[str, int, int]] = []

    for node in _walk_nodes(root):
        imported = _tree_sitter_import(language, node, content_bytes)
        if imported:
            imports.append(ParsedImport(name=imported, confidence=EXTRACTED))

        symbol = _tree_sitter_symbol(language, node, content_bytes, content)
        if symbol:
            symbols.append(symbol)
            if symbol.kind == "Function":
                function_ranges.append((symbol.name, node.start_point.row + 1, node.end_point.row + 1))

        call_name = _tree_sitter_call(language, node, content_bytes)
        if call_name:
            line = node.start_point.row + 1
            caller = _caller_for_line(function_ranges, line)
            if caller and caller != call_name:
                calls.append(ParsedCall(caller=caller, callee=call_name, line=line, confidence=INFERRED))

    # Calls to a function defined in the same file are certain: promote to EXTRACTED.
    local_functions = {s.name for s in symbols if s.kind == "Function"}
    calls = [
        ParsedCall(c.caller, c.callee, c.line, EXTRACTED if c.callee in local_functions else INFERRED)
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
    )


@lru_cache(maxsize=None)
def _tree_sitter_parser(language: str) -> Any | None:
    if Parser is None or Language is None:
        return None
    module_name, language_attr = {
        "python": ("tree_sitter_python", "language"),
        "javascript": ("tree_sitter_javascript", "language"),
        "typescript": ("tree_sitter_typescript", "language_typescript"),
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


def _walk_nodes(node: Any) -> list[Any]:
    nodes = [node]
    for child in node.children:
        nodes.extend(_walk_nodes(child))
    return nodes


def _node_text(node: Any | None, content: bytes) -> str:
    if node is None:
        return ""
    return content[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")


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


def _tree_sitter_symbol(language: str, node: Any, content_bytes: bytes, content: str) -> ParsedSymbol | None:
    kind = None
    name_node = None
    if language == "python" and node.type == "function_definition":
        kind, name_node = "Function", node.child_by_field_name("name")
    elif language == "python" and node.type == "class_definition":
        kind, name_node = "Class", node.child_by_field_name("name")
    elif language in {"javascript", "typescript"} and node.type in {"function_declaration", "method_definition"}:
        kind, name_node = "Function", node.child_by_field_name("name")
    elif language in {"javascript", "typescript"} and node.type == "class_declaration":
        kind, name_node = "Class", node.child_by_field_name("name")
    elif language == "go" and node.type in {"function_declaration", "method_declaration"}:
        kind, name_node = "Function", node.child_by_field_name("name")
    elif language == "go" and node.type == "type_spec":
        kind, name_node = "Class", node.child_by_field_name("name")
    elif language == "java" and node.type == "method_declaration":
        kind, name_node = "Function", node.child_by_field_name("name")
    elif language == "java" and node.type == "class_declaration":
        kind, name_node = "Class", node.child_by_field_name("name")
    elif language == "rust" and node.type == "function_item":
        kind, name_node = "Function", node.child_by_field_name("name")
    elif language == "rust" and node.type in {"struct_item", "enum_item", "trait_item"}:
        kind, name_node = "Class", node.child_by_field_name("name")
    elif language in {"c", "cpp"} and node.type == "function_definition":
        kind = "Function"
        name_node = _c_declarator_name(node)
    elif language == "c" and node.type == "struct_specifier":
        kind, name_node = "Class", node.child_by_field_name("name")
    elif language == "cpp" and node.type in {"class_specifier", "struct_specifier"}:
        kind, name_node = "Class", node.child_by_field_name("name")
    elif language == "ruby" and node.type == "method":
        kind, name_node = "Function", node.child_by_field_name("name")
    elif language == "ruby" and node.type in {"class", "module"}:
        kind, name_node = "Class", node.child_by_field_name("name")
    elif language == "php" and node.type in {"function_definition", "method_declaration"}:
        kind, name_node = "Function", node.child_by_field_name("name")
    elif language == "php" and node.type == "class_declaration":
        kind, name_node = "Class", node.child_by_field_name("name")

    if not kind or name_node is None:
        return None
    line = node.start_point.row + 1
    signature = content.splitlines()[line - 1].strip()
    name = _node_text(name_node, content_bytes).split("::")[-1]
    if not re.match(r"^[A-Za-z_][\w$]*$", name):
        return None
    return ParsedSymbol(
        kind=kind,
        name=name,
        line=line,
        signature=signature,
        confidence=EXTRACTED,
    )


def _c_declarator_name(node: Any) -> Any | None:
    """Descend C/C++ declarators to the identifier that names the function."""
    declarator = node.child_by_field_name("declarator")
    depth = 0
    while declarator is not None and depth < 10:
        if declarator.type in {"identifier", "field_identifier"}:
            return declarator
        if declarator.type == "qualified_identifier":
            return declarator.child_by_field_name("name") or declarator
        declarator = declarator.child_by_field_name("declarator")
        depth += 1
    return None


def _tree_sitter_call(language: str, node: Any, content: bytes) -> str | None:
    if language in {"python", "javascript", "typescript", "go"} and node.type == "call":
        return _call_function_name(_node_text(node.child_by_field_name("function"), content))
    if language in {"javascript", "typescript"} and node.type == "call_expression":
        return _call_function_name(_node_text(node.child_by_field_name("function"), content))
    if language == "go" and node.type == "call_expression":
        return _call_function_name(_node_text(node.child_by_field_name("function"), content))
    if language == "java" and node.type == "method_invocation":
        name = _node_text(node.child_by_field_name("name"), content)
        return name or None
    if language in {"rust", "c", "cpp"} and node.type == "call_expression":
        return _call_function_name(_node_text(node.child_by_field_name("function"), content))
    if language == "ruby" and node.type == "call":
        method = _node_text(node.child_by_field_name("method"), content)
        if method in {"require", "require_relative", "new"}:
            return None
        return _call_function_name(method)
    if language == "php" and node.type == "function_call_expression":
        return _call_function_name(_node_text(node.child_by_field_name("function"), content))
    if language == "php" and node.type == "member_call_expression":
        return _call_function_name(_node_text(node.child_by_field_name("name"), content))
    return None


def _call_function_name(text: str) -> str | None:
    if not text:
        return None
    name = text.split(".")[-1].split("::")[-1].split("->")[-1].lstrip("$&")
    return name if re.match(r"^[A-Za-z_][\w$]*$", name) else None


def _caller_for_line(function_ranges: list[tuple[str, int, int]], line: int) -> str | None:
    for name, start, end in reversed(function_ranges):
        if start <= line <= end:
            return name
    return None


# --- regex fallback path ----------------------------------------------------


def _parse_with_regex(path: str, content: str, language: str) -> ParsedFile:
    symbols: list[ParsedSymbol] = []
    imports: list[ParsedImport] = []
    calls: list[ParsedCall] = []
    current_function: tuple[str, int] | None = None

    for line_number, line in enumerate(content.splitlines(), start=1):
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


def _first_group(match: re.Match[str] | None) -> str | None:
    if not match:
        return None
    return next((group for group in match.groups() if group), None)
