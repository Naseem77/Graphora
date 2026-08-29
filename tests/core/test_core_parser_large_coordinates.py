"""Regression test: tree-sitter Point coordinates beyond the small-integer range.

tree-sitter 0.26.0 has a known upstream bug (already fixed, not yet released
as of writing) where reading a node's ``Point`` via the named ``.row``/
``.column`` attributes is unstable for source coordinates beyond the small
integer range (observed on real-world files such as axios's `index.d.ts`,
first triggered near line 386). Reading the same coordinates via tuple/index
access (``point[0]``, ``point[1]``) remains stable. Graphora reads points via
``graphora.parser._point_row``, which uses index access exclusively.

This fixture (`tests/fixtures/large_coordinates.ts`) is a small, locally
authored TypeScript file with two functions defined past line 256 (rows
261 and 265, 0-based) so the relevant coordinates fall outside the affected
small-integer range. The parse runs in a fresh subprocess so that any
dependency regression -- in tree-sitter itself, or in a future edit to
`graphora/parser.py` that reintroduces named attribute access -- surfaces as
an ordinary, isolated test failure rather than corrupting this test process.
"""

import json
import subprocess
import sys
from pathlib import Path

FIXTURE = Path(__file__).parent.parent / "fixtures" / "large_coordinates.ts"

# Expected (1-based) source lines, computed independently of the parser by
# construction of the fixture file itself.
EXPECTED_ALPHA_LINE = 262
EXPECTED_BETA_LINE = 266
EXPECTED_CALL_LINE = 267

_SUBPROCESS_SCRIPT = """
import json
import sys

from graphora.parser import parse_code_file

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as f:
    content = f.read()

parsed = parse_code_file("fixtures/large_coordinates.ts", content)
functions = {s.name: s.line for s in parsed.symbols if s.kind == "Function"}
calls = [
    {"caller": c.caller, "callee": c.callee, "line": c.line, "confidence": c.confidence}
    for c in parsed.calls
]
print(json.dumps({
    "parser_used": parsed.parser_used,
    "functions": functions,
    "calls": calls,
}))
"""


def test_large_row_coordinates_use_stable_point_access():
    assert FIXTURE.exists(), f"missing fixture: {FIXTURE}"

    result = subprocess.run(
        [sys.executable, "-c", _SUBPROCESS_SCRIPT, str(FIXTURE)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    assert payload["parser_used"] == "tree-sitter"

    functions = payload["functions"]
    assert functions.get("computeAlpha") == EXPECTED_ALPHA_LINE
    assert functions.get("computeBeta") == EXPECTED_BETA_LINE

    calls = payload["calls"]
    assert any(
        c["caller"] == "computeBeta"
        and c["callee"] == "computeAlpha"
        and c["line"] == EXPECTED_CALL_LINE
        for c in calls
    ), calls
