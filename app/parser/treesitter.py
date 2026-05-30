from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    from tree_sitter import Language, Parser
except ImportError:  # pragma: no cover - optional parser dependency
    Language = None  # type: ignore[assignment]
    Parser = None  # type: ignore[assignment]


DOC_EXTENSIONS = {".md", ".mdx"}
CODE_EXTENSIONS = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".java",
}


@dataclass(frozen=True)
class ParsedSymbol:
    kind: str
    name: str
    line: int
    signature: str


@dataclass(frozen=True)
class ParsedCall:
    caller: str
    callee: str
    line: int


@dataclass(frozen=True)
class ParsedDocSection:
    title: str
    level: int
    line: int
    stable_key: str


@dataclass(frozen=True)
class ParsedFile:
    path: str
    language: str
    symbols: list[ParsedSymbol]
    imports: list[str]
    calls: list[ParsedCall]
    doc_sections: list[ParsedDocSection]
    source_text: str


def is_supported_code_file(path: str) -> bool:
    return Path(path).suffix.lower() in CODE_EXTENSIONS


def is_supported_doc_file(path: str) -> bool:
    return Path(path).suffix.lower() in DOC_EXTENSIONS


def is_supported_source_file(path: str) -> bool:
    return is_supported_code_file(path) or is_supported_doc_file(path)


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
        ".md": "markdown",
        ".mdx": "markdown",
    }.get(suffix, "text")


def parse_code_file(path: str, content: str) -> ParsedFile:
    language = language_for_path(path)
    if language != "markdown":
        parsed = _parse_with_tree_sitter(path, content, language)
        if parsed is not None:
            return parsed

    return _parse_with_regex(path, content, language)


def _parse_with_regex(path: str, content: str, language: str) -> ParsedFile:
    symbols: list[ParsedSymbol] = []
    imports: list[str] = []
    calls: list[ParsedCall] = []
    doc_sections: list[ParsedDocSection] = []
    current_function: tuple[str, int] | None = None

    for line_number, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue

        if language == "markdown":
            section = _extract_doc_section(path, line_number, stripped)
            if section:
                doc_sections.append(section)
            continue

        import_name = _extract_import(language, stripped)
        if import_name:
            imports.append(import_name)

        for kind, name in _extract_symbols(language, stripped):
            symbols.append(ParsedSymbol(kind=kind, name=name, line=line_number, signature=stripped))
            if kind == "Function":
                current_function = (name, line_number)

        if current_function:
            for callee in _extract_calls(language, stripped):
                if callee != current_function[0]:
                    calls.append(ParsedCall(caller=current_function[0], callee=callee, line=line_number))

    source_text = _build_structured_text(path, language, symbols, imports, calls, doc_sections, content)
    return ParsedFile(
        path=path,
        language=language,
        symbols=symbols,
        imports=imports,
        calls=calls,
        doc_sections=doc_sections,
        source_text=source_text,
    )


def _parse_with_tree_sitter(path: str, content: str, language: str) -> ParsedFile | None:
    parser = _tree_sitter_parser(language)
    if parser is None:
        return None

    content_bytes = content.encode("utf-8")
    tree = parser.parse(content_bytes)
    root = tree.root_node
    symbols: list[ParsedSymbol] = []
    imports: list[str] = []
    calls: list[ParsedCall] = []
    function_ranges: list[tuple[str, int, int]] = []

    for node in _walk_nodes(root):
        imported = _tree_sitter_import(language, node, content_bytes)
        if imported:
            imports.append(imported)

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
                calls.append(ParsedCall(caller=caller, callee=call_name, line=line))

    source_text = _build_structured_text(path, language, symbols, imports, calls, [], content)
    return ParsedFile(
        path=path,
        language=language,
        symbols=symbols,
        imports=_dedupe(imports),
        calls=_dedupe_calls(calls),
        doc_sections=[],
        source_text=source_text,
    )


def extract_symbols_from_diff(diff: str) -> list[str]:
    symbols: list[str] = []
    seen: set[str] = set()
    for line in diff.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        for _, name in _extract_symbols("python", line[1:].strip()):
            if name not in seen:
                seen.add(name)
                symbols.append(name)
        for language in ("typescript", "go", "java"):
            for _, name in _extract_symbols(language, line[1:].strip()):
                if name not in seen:
                    seen.add(name)
                    symbols.append(name)
    return symbols


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
    return None


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
    return None


def _tree_sitter_symbol(language: str, node: Any, content_bytes: bytes, content: str) -> ParsedSymbol | None:
    kind = None
    name_node = None
    if language == "python" and node.type == "function_definition":
        kind = "Function"
        name_node = node.child_by_field_name("name")
    elif language == "python" and node.type == "class_definition":
        kind = "Class"
        name_node = node.child_by_field_name("name")
    elif language in {"javascript", "typescript"} and node.type in {"function_declaration", "method_definition"}:
        kind = "Function"
        name_node = node.child_by_field_name("name")
    elif language in {"javascript", "typescript"} and node.type == "class_declaration":
        kind = "Class"
        name_node = node.child_by_field_name("name")
    elif language == "go" and node.type in {"function_declaration", "method_declaration"}:
        kind = "Function"
        name_node = node.child_by_field_name("name")
    elif language == "go" and node.type == "type_spec":
        kind = "Class"
        name_node = node.child_by_field_name("name")
    elif language == "java" and node.type == "method_declaration":
        kind = "Function"
        name_node = node.child_by_field_name("name")
    elif language == "java" and node.type == "class_declaration":
        kind = "Class"
        name_node = node.child_by_field_name("name")

    if not kind or name_node is None:
        return None

    line = node.start_point.row + 1
    signature = content.splitlines()[line - 1].strip()
    return ParsedSymbol(kind=kind, name=_node_text(name_node, content_bytes), line=line, signature=signature)


def _tree_sitter_call(language: str, node: Any, content: bytes) -> str | None:
    if language in {"python", "javascript", "typescript", "go"} and node.type == "call":
        return _call_function_name(_node_text(node.child_by_field_name("function"), content))
    if language == "java" and node.type == "method_invocation":
        name = _node_text(node.child_by_field_name("name"), content)
        return name or None
    return None


def _call_function_name(text: str) -> str | None:
    if not text:
        return None
    name = text.split(".")[-1]
    return name if re.match(r"^[A-Za-z_][\w$]*$", name) else None


def _caller_for_line(function_ranges: list[tuple[str, int, int]], line: int) -> str | None:
    for name, start, end in reversed(function_ranges):
        if start <= line <= end:
            return name
    return None


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def _dedupe_calls(calls: list[ParsedCall]) -> list[ParsedCall]:
    deduped: list[ParsedCall] = []
    seen: set[tuple[str, str, int]] = set()
    for call in calls:
        key = (call.caller, call.callee, call.line)
        if key not in seen:
            seen.add(key)
            deduped.append(call)
    return deduped


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
    }

    found: list[tuple[str, str]] = []
    for kind, pattern in patterns.get(language, []):
        match = re.match(pattern, line)
        if match:
            found.append((kind, match.group(1)))
    return found


def _extract_calls(language: str, line: str) -> list[str]:
    if language not in {"python", "javascript", "typescript", "go", "java"}:
        return []
    ignored = {
        "if",
        "for",
        "while",
        "switch",
        "return",
        "class",
        "def",
        "function",
        "catch",
        "new",
    }
    calls = []
    for match in re.finditer(r"\b([A-Za-z_][\w$]*)\s*\(", line):
        name = match.group(1)
        if name not in ignored and not name[0].isupper():
            calls.append(name)
    return calls


def _extract_doc_section(path: str, line_number: int, line: str) -> ParsedDocSection | None:
    match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
    if not match:
        return None
    title = match.group(2).strip()
    return ParsedDocSection(
        title=title,
        level=len(match.group(1)),
        line=line_number,
        stable_key=f"{path}:Section:{title.lower().replace(' ', '-')}",
    )


def _build_structured_text(
    path: str,
    language: str,
    symbols: list[ParsedSymbol],
    imports: list[str],
    calls: list[ParsedCall],
    doc_sections: list[ParsedDocSection],
    content: str,
) -> str:
    symbol_lines = "\n".join(
        f"- {symbol.kind}: {symbol.name} at line {symbol.line}; signature: {symbol.signature}"
        for symbol in symbols
    )
    import_lines = "\n".join(f"- {item}" for item in imports)
    call_lines = "\n".join(f"- {call.caller} calls {call.callee} at line {call.line}" for call in calls)
    section_lines = "\n".join(
        f"- h{section.level}: {section.title} at line {section.line}"
        for section in doc_sections
    )
    return (
        f"File: {path}\n"
        f"Language: {language}\n"
        f"Imports:\n{import_lines or '- none'}\n"
        f"Symbols:\n{symbol_lines or '- none'}\n"
        f"Calls:\n{call_lines or '- none'}\n"
        f"Doc sections:\n{section_lines or '- none'}\n\n"
        f"Source:\n{content}"
    )


def _first_group(match: re.Match[str] | None) -> str | None:
    if not match:
        return None
    return next((group for group in match.groups() if group), None)
