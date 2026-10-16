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


def _run(capsys, *argv) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


EMB = ("--project", "p", "--backend", "embedded")


def test_depth_must_be_positive(indexed, capsys):
    for depth in ("0", "-1"):
        with pytest.raises(SystemExit) as exc:
            main(["breaks", "run", "--depth", depth, *EMB])
        assert exc.value.code == 2
    assert "must be at least 1" in capsys.readouterr().err


def test_unknown_symbol_exits_nonzero(indexed, capsys):
    code, out, _ = _run(capsys, "breaks", "nope", *EMB)
    assert code == 1 and "No definition found for `nope`" in out
    code, _, _ = _run(capsys, "blast", "nope", *EMB)
    assert code == 1


def test_diff_touching_no_symbol_is_worded_plainly(indexed, tmp_path, capsys):
    diff = tmp_path / "d.diff"
    diff.write_text("--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-a\n+b\n")
    code, out, _ = _run(capsys, "breaks", "--diff", str(diff), *EMB)
    assert code == 0
    assert "touches no function, method, or class" in out and "nothing" not in out


def test_errors_are_one_line_not_tracebacks(indexed, tmp_path, capsys):
    code, _, err = _run(capsys, "breaks", "--diff", str(tmp_path / "missing.diff"), *EMB)
    assert code == 1 and err.startswith("graphora: error:") and "Traceback" not in err
    code, _, err = _run(capsys, "breaks", "--git-range", "nope..nada", "--repo", str(tmp_path), *EMB)
    assert code == 1 and err.startswith("graphora: error:") and "Traceback" not in err
    code, _, err = _run(capsys, "stats", "--project", "p", "--backend", "falkordb", "--port", "1")
    assert code == 1 and "cannot reach FalkorDB" in err


def test_read_commands_on_an_empty_graph_fail_without_creating_it(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("GRAPHORA_DATA_DIR", str(tmp_path / "data"))
    code, _, err = _run(capsys, "stats", "--project", "never", "--backend", "embedded")
    assert code == 1 and "is empty" in err
    assert not (tmp_path / "data" / "never.json").exists()


def test_falkordb_read_does_not_create_a_graph(capsys):
    from graphora.store import falkordb_available

    if not falkordb_available():
        pytest.skip("FalkorDB not running on localhost:6379")
    from falkordb import FalkorDB

    name = "graphora-never-created"
    if f"graphora:{name}" in FalkorDB().list_graphs():
        FalkorDB().select_graph(f"graphora:{name}").delete()
    assert main(["stats", "--project", name, "--backend", "falkordb"]) == 1
    assert f"graphora:{name}" not in FalkorDB().list_graphs()


def test_install_skill_refuses_a_missing_repo(tmp_path, capsys):
    code, _, err = _run(capsys, "install-skill", "all", "--repo", str(tmp_path / "nope"))
    assert code == 1 and "does not exist" in err
    assert not (tmp_path / "nope").exists()


def test_broken_pipe_is_quiet(indexed, monkeypatch, capsys):
    class ClosedPipe:
        def write(self, _text):
            raise BrokenPipeError

        def flush(self):
            raise BrokenPipeError

    monkeypatch.setattr("sys.stdout", ClosedPipe())
    assert main(["breaks", "run", *EMB]) == 0
