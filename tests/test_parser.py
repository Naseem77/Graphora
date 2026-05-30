from app.parser.treesitter import extract_symbols_from_diff, parse_code_file


def test_parse_python_symbols_and_imports():
    parsed = parse_code_file("app/example.py", "import os\n\nclass User:\n    pass\n\ndef login(user):\n    return user\n")

    assert parsed.language == "python"
    assert parsed.imports == ["os"]
    assert [symbol.name for symbol in parsed.symbols] == ["User", "login"]
    assert "File: app/example.py" in parsed.source_text


def test_extract_symbols_from_diff():
    diff = """
diff --git a/app/example.py b/app/example.py
+def new_function():
+    return True
+class NewClass:
+    pass
"""

    assert extract_symbols_from_diff(diff) == ["new_function", "NewClass"]
