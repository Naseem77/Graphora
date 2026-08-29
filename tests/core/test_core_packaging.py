"""Package-metadata assertions for the tree-sitter dependency bound.

tree-sitter 0.26.0 is excluded (see `pyproject.toml` and
`graphora/parser.py::_point_row`) because of an upstream Point-coordinate
bug. These tests assert the exclusion is present in the installed package's
metadata -- so if the bound is accidentally dropped or widened, a normal
test failure catches it -- while leaving room for any future corrected
release above 0.26.0.
"""

import importlib.metadata as metadata
import re

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet


def _tree_sitter_requirement() -> Requirement:
    requires = metadata.requires("graphora-kg") or []
    for raw in requires:
        req = Requirement(raw)
        if req.name == "tree-sitter":
            return req
    raise AssertionError("graphora-kg metadata does not declare a tree-sitter requirement")


def test_tree_sitter_026_is_excluded():
    req = _tree_sitter_requirement()
    specifier = req.specifier
    assert "0.26.0" not in specifier, "tree-sitter 0.26.0 must stay excluded (unstable Point coordinates)"
    assert not specifier.contains("0.26.0", prereleases=True)


def test_tree_sitter_lower_bound_and_future_releases_allowed():
    req = _tree_sitter_requirement()
    specifier: SpecifierSet = req.specifier
    # The historical floor stays installable...
    assert specifier.contains("0.23.0", prereleases=True)
    assert specifier.contains("0.25.2", prereleases=True)
    # ...and a hypothetical corrected release after 0.26.0 stays supportable.
    assert specifier.contains("0.26.1", prereleases=True)
    assert specifier.contains("0.27.0", prereleases=True)


def test_pyproject_declares_matching_exclusion():
    # Cross-check the source declaration too, so the test doesn't only pass
    # against a stale installed .dist-info from a previous `pip install -e`.
    from pathlib import Path

    pyproject = Path(__file__).parent.parent.parent / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    # Matches the dependency entry (e.g. "tree-sitter>=0.23,!=0.26.0") while
    # skipping the bare "tree-sitter" keyword and "tree-sitter-<lang>" extras.
    match = re.search(r'"tree-sitter(>=[^"]*)"', text)
    assert match, "tree-sitter dependency line not found in pyproject.toml"
    assert "!=0.26.0" in match.group(1)
