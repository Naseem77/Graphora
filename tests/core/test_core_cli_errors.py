"""CLI and indexer failure modes: clear errors, correct exit codes, no data loss."""

from pathlib import Path

import pytest

from graphora.cli import main
from graphora.embedded import EmbeddedGraphStore
from graphora.indexer import index_repository


@pytest.fixture()
def indexed(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GRAPHORA_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def helper():\n    return 1\n\ndef run():\n    return helper()\n")
    assert main(["index", str(repo), "--project", "p", "--backend", "embedded"]) == 0
    return repo


def _files(tmp_path: Path) -> int:
    return EmbeddedGraphStore("p", data_dir=tmp_path / "data").stats()["files"]


def test_index_of_a_missing_path_fails_and_keeps_the_graph(indexed, tmp_path, capsys):
    capsys.readouterr()
    assert main(["index", str(tmp_path / "typo"), "--project", "p", "--backend", "embedded"]) == 1
    assert "does not exist" in capsys.readouterr().err
    assert _files(tmp_path) == 1


def test_index_of_a_file_or_empty_dir_fails_and_keeps_the_graph(indexed, tmp_path, capsys):
    (tmp_path / "empty").mkdir()
    assert main(["index", str(indexed / "a.py"), "--project", "p", "--backend", "embedded"]) == 1
    assert main(["index", str(tmp_path / "empty"), "--project", "p", "--backend", "embedded"]) == 1
    assert "no supported source files" in capsys.readouterr().err
    assert _files(tmp_path) == 1


def test_library_index_raises_instead_of_wiping(indexed, tmp_path):
    store = EmbeddedGraphStore("p", data_dir=tmp_path / "data")
    with pytest.raises(FileNotFoundError):
        index_repository(tmp_path / "typo", project="p", store=store)
    assert store.stats()["files"] == 1
