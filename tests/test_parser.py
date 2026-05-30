from app.parser.treesitter import extract_symbols_from_diff, is_supported_doc_file, parse_code_file


def test_parse_python_symbols_and_imports():
    parsed = parse_code_file("app/example.py", "import os\n\nclass User:\n    pass\n\ndef login(user):\n    return user\n")

    assert parsed.language == "python"
    assert parsed.imports == ["os"]
    assert [symbol.name for symbol in parsed.symbols] == ["User", "login"]
    assert parsed.calls == []
    assert "File: app/example.py" in parsed.source_text


def test_parse_calls_and_markdown_sections():
    parsed = parse_code_file("app/example.py", "def helper():\n    pass\n\ndef login():\n    return helper()\n")
    docs = parse_code_file("README.md", "# Title\n\n## Usage\n")

    assert [call.callee for call in parsed.calls] == ["helper"]
    assert is_supported_doc_file("README.md")
    assert [section.title for section in docs.doc_sections] == ["Title", "Usage"]


def test_extract_symbols_from_diff():
    diff = """
diff --git a/app/example.py b/app/example.py
+def new_function():
+    return True
+class NewClass:
+    pass
"""

    assert extract_symbols_from_diff(diff) == ["new_function", "NewClass"]
