from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


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
class ParsedFile:
    path: str
    language: str
    symbols: list[ParsedSymbol]
    imports: list[str]
    source_text: str


def is_supported_code_file(path: str) -> bool:
    return Path(path).suffix.lower() in CODE_EXTENSIONS


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
    }.get(suffix, "text")


def parse_code_file(path: str, content: str) -> ParsedFile:
    language = language_for_path(path)
    symbols: list[ParsedSymbol] = []
    imports: list[str] = []

    for line_number, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue

        import_name = _extract_import(language, stripped)
        if import_name:
            imports.append(import_name)

        for kind, name in _extract_symbols(language, stripped):
            symbols.append(ParsedSymbol(kind=kind, name=name, line=line_number, signature=stripped))

    source_text = _build_structured_text(path, language, symbols, imports, content)
    return ParsedFile(path=path, language=language, symbols=symbols, imports=imports, source_text=source_text)


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


def _build_structured_text(
    path: str,
    language: str,
    symbols: list[ParsedSymbol],
    imports: list[str],
    content: str,
) -> str:
    symbol_lines = "\n".join(
        f"- {symbol.kind}: {symbol.name} at line {symbol.line}; signature: {symbol.signature}"
        for symbol in symbols
    )
    import_lines = "\n".join(f"- {item}" for item in imports)
    return (
        f"File: {path}\n"
        f"Language: {language}\n"
        f"Imports:\n{import_lines or '- none'}\n"
        f"Symbols:\n{symbol_lines or '- none'}\n\n"
        f"Source:\n{content}"
    )


def _first_group(match: re.Match[str] | None) -> str | None:
    if not match:
        return None
    return next((group for group in match.groups() if group), None)
