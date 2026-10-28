"""OpenBCA hosted by ReCET (recet/openbca): finding a checkout, staging a run's copy of it, the command line. These need no OpenBCA install; the one
test that runs OpenBCA itself (comparison C1 on its demo workbooks) runs only when RECET_OPENBCA_HOME names a synced checkout."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from recet.openbca import run


def _fake_checkout(root: Path) -> run.Checkout:
    for d in run.PROJECT_DIRS:
        (root / d).mkdir(parents=True)
    (root / "core" / "config.yaml").write_text("gateways: {}\n")
    (root / "core" / ".cache").mkdir()
    (root / "core" / ".cache" / "x").write_text("stale")
    (root / "excel_input_parsing" / "input_templates").mkdir()
    (root / "excel_input_parsing" / "input_templates" / run.PROGRAM_INPUT).write_bytes(b"someone else's job")
    return run.Checkout(root, Path(sys.executable))


def test_find_checkout_needs_a_checkout_and_its_interpreter(tmp_path, monkeypatch):
    monkeypatch.delenv("RECET_OPENBCA_HOME", raising=False)
    monkeypatch.delenv("RECET_OPENBCA_PYTHON", raising=False)
    with pytest.raises(FileNotFoundError, match="RECET_OPENBCA_HOME"):
        run.find_checkout()
    with pytest.raises(FileNotFoundError, match="not an OpenBCA checkout"):
        run.find_checkout(tmp_path)
    _fake_checkout(tmp_path / "ob")
    with pytest.raises(FileNotFoundError, match="uv sync"):
        run.find_checkout(tmp_path / "ob")
    got = run.find_checkout(tmp_path / "ob", python=sys.executable)
    assert got.home == (tmp_path / "ob").resolve() and got.python == Path(sys.executable)


def test_stage_copies_the_projects_and_puts_the_workbooks_under_openbcas_names(tmp_path):
    co = _fake_checkout(tmp_path / "ob")
    (tmp_path / "mine.xlsm").write_bytes(b"program input")
    (tmp_path / "cfg.xlsx").write_bytes(b"configuration")
    ws = run.stage(co, tmp_path / "run" / "openbca", tmp_path / "mine.xlsm", tmp_path / "cfg.xlsx")
    templates = ws / "excel_input_parsing" / "input_templates"
    assert (templates / run.PROGRAM_INPUT).read_bytes() == b"program input"
    assert (templates / run.CONFIGURATION).read_bytes() == b"configuration"
    assert (ws / "core" / "config.yaml").exists() and (ws / "engine.py").exists()
    assert not (ws / "core" / ".cache").exists()  # no SQLmesh state carried in from the checkout
    # the checkout itself is never written to
    assert (tmp_path / "ob" / "excel_input_parsing" / "input_templates" / run.PROGRAM_INPUT).read_bytes() == b"someone else's job"


def test_engine_is_not_imported_by_recet():
    """engine.py runs in OpenBCA's interpreter; importing recet.openbca must not need SQLmesh or DuckDB."""
    import recet.openbca.run  # noqa: F401
    assert "sqlmesh" not in sys.modules


def test_cli_reports_a_missing_checkout(monkeypatch, capsys):
    from recet import cli
    monkeypatch.delenv("RECET_OPENBCA_HOME", raising=False)
    with pytest.raises(FileNotFoundError):
        cli.main(["openbca", "compare", "a.db", "b.db"])


@pytest.mark.skipif(not os.environ.get("RECET_OPENBCA_HOME"), reason="needs RECET_OPENBCA_HOME (a synced OpenBCA checkout)")
def test_c1_hosted_run_of_the_demo_matches_a_second_run(tmp_path):
    """C1 in small: two hosted runs of OpenBCA's demo workbooks give the same tables (the full C1 compares with OpenBCA's own `make run-openbca-model`)."""
    co = run.find_checkout()
    demo = co.home / "demo_input_files"
    a = run.run_openbca(demo / run.PROGRAM_INPUT, demo / run.CONFIGURATION, tmp_path, "a", checkout=co)
    b = run.run_openbca(demo / run.PROGRAM_INPUT, demo / run.CONFIGURATION, tmp_path, "b", checkout=co)
    assert "openbca_results_summary_by_id" in a.files and "openbca_jst_ratio" in a.files
    assert run.compare_dbs(a.run_dir / "openbca" / "openbca.db", b.run_dir / "openbca" / "openbca.db", co)


def test_a_relative_checkout_gives_an_absolute_interpreter(tmp_path, monkeypatch):
    """The engine runs with cwd = the run's workspace, so a relative interpreter path would point nowhere."""
    _fake_checkout(tmp_path / "ob")
    py = tmp_path / "ob" / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("")
    monkeypatch.chdir(tmp_path)
    got = run.find_checkout("ob", python="ob/.venv/bin/python")
    assert got.python.is_absolute() and got.python == py
